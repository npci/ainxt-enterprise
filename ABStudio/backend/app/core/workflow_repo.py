# SPDX-License-Identifier: Apache-2.0
"""
Workflow repository  -- persistent CRUD for workflows and templates.
 
Backends
  PostgreSQL   activated when POSTGRES_HOST is set. Reuses the platform's
               single shared pool (db.database.engine) via
               app.core.db_pool.SHARED_POOL — no separate pool is created.
  In-memory    fallback when no host is configured; data is lost on restart.
               Suitable for local development / demo use.
 
Data model
  Workflow   owned by a user (user_id), contains a graph_data JSON blob
             (nodes + edges), name, and audit timestamps.
  Template   read-only seed records. Users clone them via use_template()
             which creates a personal copy and returns it.
 
Key public API
  init_db()                                   -- create tables, seed templates
  close_db()                                  -- close the connection pool
  get_all_workflows(user_id)                 → list of workflow dicts
  create_workflow(data, user_id, full_name)  → workflow dict
  get_workflow(workflow_id, user_id)         → workflow dict or None
  update_workflow(workflow_id, data, user_id)→ workflow dict or None
  delete_workflow(workflow_id, user_id)
  duplicate_workflow(workflow_id, …)         → workflow dict or None
  get_all_templates()                        → list of template dicts
  get_template(template_id)                  → template dict or None
  use_template(template_id, user_id, …)      → new workflow dict or None
 
Used by: main.py (workflow CRUD and template endpoints)
"""
import os
import copy
import json
import uuid
import asyncio
import importlib.util
import logging
import re
import pprint
import textwrap
import threading
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
 
from core.logger import logger, LOG_LEVEL as _LOG_LEVEL

from app.core.config import postgres_enabled
from app.core.skill_manifest import NO_SUBDIRS_CLAUSE
from app.core.kb_retriever import KB_MODE_NONE

# Template model resolver: reads the same env vars the registry uses, so a
# deployment that configured its own models gets templates that reference them --
# previously a seeded template named a shipped cloud id the adopter had no
# credentials for, and the workflow failed at run time rather than at seed time.
def _tmpl_model(env_var: str, shipped: str = "") -> str:
    """Return the env-var value for a model, or ``shipped`` if unset.

    ``shipped`` defaults to ``""`` — callers that pass a concrete model ID
    as the fallback should migrate to ``""`` so no model name is hardcoded.
    """
    return os.getenv(env_var, shipped)

# Canonical "no KB attached" blob — shared default for the agents.knowledge
# and workflows.knowledge JSONB columns. Defined once so the dozen sites
# that need to seed a fresh row don't drift on the spelling.
_KB_DEFAULT_BLOB: Dict[str, Any] = {"mode": KB_MODE_NONE}
_pool = None   # shared pool (db_pool.SHARED_POOL) -- bound in init_db()

# ---------------------------------------------------------------------------
# Catalog cache (REQ-P3-1)
#
# ``tools_catalog`` / ``skills_catalog`` / ``skill_files`` rows only change
# via the tool editor / skill factory / skill uploader -- rare, admin-driven
# events. Every workflow node re-read them from Postgres on every execution
# (and every loop iteration multiplied that). A plain in-process dict cache
# with mutation-based invalidation removes that DB round-trip for the common
# case (rows already read once this process) while staying correct: every
# mutator below invalidates its own key(s) after a successful write, so a
# stale entry cannot survive an edit. No TTL is needed for the same reason.
#
# Stale-write race (post-review fix): the DB round-trip runs OUTSIDE the
# lock (it hops to a thread), so a reader can be mid-flight with pre-edit
# data at the exact moment a writer commits and invalidates. Without a
# guard the reader would then write that stale row back into the cache
# right after the invalidation, silently undoing it. Each cache family
# carries a generation counter, bumped under the lock by its invalidator;
# a reader snapshots the generation before starting its DB call and only
# writes its result back if the generation hasn't moved. A losing reader
# doesn't retry -- it just leaves the cache cold for that key, which is
# self-healing on the next call.
#
# Cache entries are deep-copied on the way in AND out: the rows here go on
# to be threaded through ``_CatalogTool`` wrappers, rendered into prompts,
# and passed to LLM clients that mutate the JSON-schema dict in place
# (schema "fix-up" helpers). Handing out a shared reference would let any
# one of those call sites corrupt the process-wide cache for every later
# workflow run. Deep copies are cheap here -- these rows are small
# (tool/skill source + short metadata) -- so the safety is effectively
# free.
#
# Per-process only -- a multi-worker deployment won't see another worker's
# edit until that worker's own cache entry is evicted by its own mutator
# call (or the worker restarts). Acceptable because catalog edits are rare
# and admin-driven; see the requirements doc (REQ-P3-1) for the full
# rationale and the existing code_executor/read_skill_file singleton cache
# that already carries the same property.
# ---------------------------------------------------------------------------
_CATALOG_CACHE_LOCK = threading.Lock()
_tool_cache: Dict[str, Optional[Dict[str, Any]]] = {}
_skill_cache: Dict[str, Optional[Dict[str, Any]]] = {}
_skill_files_cache: Dict[str, List[Dict[str, Any]]] = {}


class _CacheGeneration:
    """A mutable counter cell so ``_cached_catalog_read`` can share one
    generation reference across a family (e.g. ``_skill_cache`` and
    ``_skill_files_cache`` both bump the SAME counter, since
    ``_invalidate_skill_cache`` always evicts both together)."""
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0


_tool_cache_generation = _CacheGeneration()
_skill_cache_generation = _CacheGeneration()  # shared by skills + skill files


def _invalidate_tool_cache(name: Optional[str] = None) -> None:
    """Evict a tool (or, when ``name`` is None, every tool) from the cache."""
    with _CATALOG_CACHE_LOCK:
        _tool_cache_generation.value += 1
        if name is None:
            _tool_cache.clear()
        else:
            _tool_cache.pop(name, None)


def _invalidate_skill_cache(name: Optional[str] = None) -> None:
    """Evict a skill + its file manifest (or everything) from the cache."""
    with _CATALOG_CACHE_LOCK:
        _skill_cache_generation.value += 1
        if name is None:
            _skill_cache.clear()
            _skill_files_cache.clear()
        else:
            _skill_cache.pop(name, None)
            _skill_files_cache.pop(name, None)


async def _cached_catalog_read(
    cache: Dict[Any, Any],
    generation: _CacheGeneration,
    key: Any,
    fetch,
) -> Any:
    """Shared get-or-fetch-with-cache body for ``get_tool`` / ``get_skill`` /
    ``list_skill_files``.

    Returns a private deep copy on every path so the caller can never
    mutate the cache's own copy (see the module-level cache docstring
    above). ``fetch`` is a zero-arg callable returning an awaitable (the
    DB round-trip); it always runs OUTSIDE the lock. The generation
    snapshot/compare closes the stale-write race: if a mutator invalidates
    this family while ``fetch`` is in flight, the result is discarded
    instead of being written back over the (now-correct) empty cache slot.
    """
    with _CATALOG_CACHE_LOCK:
        if key in cache:
            return copy.deepcopy(cache[key])
        start_generation = generation.value

    result = await fetch()

    with _CATALOG_CACHE_LOCK:
        if generation.value == start_generation:
            cache[key] = copy.deepcopy(result)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_uri():
    """Guard that ABStudio persistence is available (name kept for callers)."""
    if not postgres_enabled():
        raise RuntimeError(
            "POSTGRES_HOST is not set. Configure the platform database "
            "and restart the backend."
        )


def _get_pool():
    if _pool is None:
        raise RuntimeError(
            "DB pool not ready  -- check POSTGRES_HOST and restart."
        )
    return _pool
 
 
def get_pool():
    """Return the pool, or None when running in in-memory fallback mode."""
    return _pool


# Public aliases used by peer repositories that share this pool. They wrap
# the underscored internals so cross-module callers don't have to reach into
# private names.
require_uri = _require_uri


def get_pool_or_raise():
    """Public alternative to ``_get_pool``: raise if the shared pool isn't up."""
    return _get_pool()


def new_prefixed_id(prefix: str) -> str:
    """Generate a stable ``{prefix}-{12hex}`` identifier used across repos."""
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:12]}"

 
def _row_to_workflow(row) -> Dict[str, Any]:
    # ``knowledge`` and ``source_template_id`` are appended at the tail of
    # every SELECT list so older rows (and the dashboard list SELECT that
    # intentionally omits them to save bandwidth) still deserialise into
    # safe defaults instead of raising IndexError.
    knowledge = (
        row[8] if len(row) > 8 and row[8] is not None else dict(_KB_DEFAULT_BLOB)
    )
    source_template_id = (
        row[9] if len(row) > 9 and row[9] is not None else None
    )
    return {
        "id":                 row[0],
        "name":               row[1],
        "description":        row[2],
        "author":             row[3],
        "graphData":          row[4],
        "created_at":         row[5].isoformat() if row[5] else None,
        "updated_at":         row[6].isoformat() if row[6] else None,
        "owner_user_id":      row[7],
        "knowledge":          knowledge,
        "source_template_id": source_template_id,
    }
 
 
def _row_to_template(row) -> Dict[str, Any]:
    # Newer columns (`pattern`, `hitl`) are appended at the end of the SELECT
    # list so legacy rows that predate the ALTER still come back from the
    # in-memory fallback path with the right shape — the trailing tuple
    # slots fall back to safe defaults.
    pattern = row[5] if len(row) > 5 and row[5] is not None else "sequential"
    hitl    = bool(row[6]) if len(row) > 6 and row[6] is not None else False
    # visibility/department appended at the tail so callers/selects that predate
    # the Deploy-to-templates feature still map cleanly (default = public).
    visibility = row[7] if len(row) > 7 and row[7] is not None else "public"
    department = row[8] if len(row) > 8 else None
    return {
        "id":          row[0],
        "name":        row[1],
        "description": row[2],
        "category":    row[3],
        "graphData":   row[4],
        "pattern":     pattern,
        "hitl":        hitl,
        "visibility":  visibility,
        "department":  department,
    }
 
 
# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
_DSLAR_READ_EXTRACTED_SNIPPET = """

Mandatory code_executor pattern for extracted.json:
```python
import json
from pathlib import Path

work_dir = Path(WORKFLOW_ARTIFACT_DIR)
extracted_path = work_dir / "extracted.json"
payload = json.loads(extracted_path.read_text(encoding="utf-8"))
# inspect or update payload as instructed, then write it back when changed
extracted_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"artifact_dir": str(work_dir), "extracted_json": str(extracted_path), "status": "content_extraction_complete"}, ensure_ascii=False))
```
"""

_DSLAR_READ_ENRICHED_SNIPPET = """

Mandatory code_executor pattern to read enriched.json:
```python
import json
from pathlib import Path

work_dir = Path(WORKFLOW_ARTIFACT_DIR)
enriched_path = work_dir / "enriched.json"
payload = json.loads(enriched_path.read_text(encoding="utf-8"))
extracted = payload.get("extracted", {})
evidence = {
    "metadata_checks": payload.get("metadata_checks", {}),
    "points_not_concluded": payload.get("points_not_concluded", []),
    "validation_type": payload.get("validation_type"),
    "full_text": (extracted.get("full_text") or "")[:50000],
    "sections": (extracted.get("sections") or [])[:50],
    "tables": [{**t, "rows": (t.get("rows") or [])[:50]} for t in (extracted.get("tables") or [])[:20] if isinstance(t, dict)],
    "images": [{"page": i.get("page"), "ref": i.get("ref"), "xref": i.get("xref"), "description": i.get("description") or "", "description_status": i.get("description_status"), "description_error": i.get("description_error"), "description_response_preview": i.get("description_response_preview")} for i in (extracted.get("images") or [])[:100] if isinstance(i, dict)],
}
print(json.dumps(evidence, ensure_ascii=False))
```
"""

_DSLAR_UPDATE_ENRICHED_SNIPPET = """

Mandatory code_executor pattern to update enriched.json:
```python
import json
from pathlib import Path

work_dir = Path(WORKFLOW_ARTIFACT_DIR)
enriched_path = work_dir / "enriched.json"
payload = json.loads(enriched_path.read_text(encoding="utf-8"))
# update payload with the fields produced by this agent, for example:
# payload["metadata_checks"] = metadata_checks
# payload["points_not_concluded"] = points_not_concluded
# payload["route"] = route
# payload["validation_type"] = validation_type
enriched_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
status = "<node_specific_status>"
print(json.dumps({"artifact_dir": str(work_dir), "enriched_json": str(enriched_path), "validation_type": payload.get("validation_type"), "route": payload.get("route"), "status": status}, ensure_ascii=False))
```
"""

_DSLAR_CHUNK_SNIPPET = """

Page-chunked map-reduce is MANDATORY and is fully specified by the `dslar-clause-chunking` skill. BEFORE any code_executor call you MUST call `read_skill_file("dslar-clause-chunking", "SKILL.md")` to load the split / read-batch / reduce workflow, the bundled script's absolute path, the per-chunk partial shapes, and the present-if-any reduce semantics. Then run the bundled `scripts/chunk_dslar_pages.py` via code_executor/runpy exactly as the skill documents — do NOT hand-roll your own chunking or read enriched.json in one shot, which truncates evidence on later pages.

This branch is parameterised as follows (the skill describes the mechanics; these are the values to plug in):
- BRANCH_DIR (private per-branch I/O dir, prevents the four parallel validators clobbering each other): os.path.join(WORKFLOW_ARTIFACT_DIR, "_chunk_{branch_tag}"). Create it before splitting and pass it as --work-dir for every mode (split / read-batch / reduce). Source enriched.json stays at WORKFLOW_ARTIFACT_DIR/enriched.json; never write chunk_*.json, partials.json, or result.json into WORKFLOW_ARTIFACT_DIR itself.
- --chunk-pages {chunk_pages} for the split step.

CHECKPOINTING IS NON-NEGOTIABLE — a branch that never writes BRANCH_DIR/partials.json yields a blank "not concluded" verdict for every clause, because the aggregator can only recover what is on disk. The chat fan-in does NOT carry your reasoning to the aggregator; ONLY the files in BRANCH_DIR do. Therefore:
- The FIRST thing you do after reasoning over each read-batch — before requesting the next batch — is overwrite BRANCH_DIR/partials.json with the FULL accumulated flat list of every per-chunk partial recorded so far (across all batches). Do this every batch, not just at the end. If this node runs out of iteration budget mid-loop, the last checkpoint is what gets reduced, so a partially-evaluated branch is still salvaged instead of going blank.
- After the final batch, run --mode reduce and, in the SAME code_executor call, write the final branch update to BRANCH_DIR/result.json as {{"clause_results": [...], "points_not_concluded": [...]}} (Clause 1 carries its data_element_results inside its single clause_results entry). The aggregator prefers result.json and falls back to partials.json.
- partials.json and result.json are the ONLY files you may write to BRANCH_DIR besides the script's own chunk_*.json. Do NOT dump chunk evidence, batch responses, or scratch notes into other filenames (e.g. all_chunks_data.json, *_full.json) — that wastes iterations and produces nothing the aggregator reads, which is exactly how a branch ends up blank.
- Do NOT write enriched.json from this node — the aggregator is its single writer. Return the result.json object as your final message.
"""


_SEED_TEMPLATES = [
    # ── 1. SDLC Feature Flow ─────────────────────────────────────────────
    {
        "id": "template-sdlc-feature-flow",
        "name": "SDLC Feature Flow",
        "description": "End-to-end feature delivery: read a Jira ticket, plan the implementation, create a GitLab branch and merge request",
        "category": "Engineering",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "feature-analyst", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Feature Analyst",
                        "instructions": (
                            "You are a senior feature analyst. Given a Jira issue key, fetch the full "
                            "ticket details (summary, description, acceptance criteria). Analyse the "
                            "requirements and produce a concise implementation plan covering the files "
                            "to change, the approach, and any risks. Post your plan as a Jira comment "
                            "so stakeholders can review it."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
                            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
                        ],
                    },
                },
                {
                    "id": "developer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Developer",
                        "instructions": (
                            "You are an experienced developer. Using the implementation plan from the "
                            "previous step, create a feature branch in GitLab from main, apply the "
                            "necessary code changes, and open a merge request. The MR title should "
                            "reference the Jira ticket key and the MR description should summarise "
                            "the changes made."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "gitlab_create_branch", "description": "Create a new branch in a GitLab repo"},
                            {"name": "gitlab_create_or_update_file", "description": "Create or update a file in a GitLab repo"},
                            {"name": "gitlab_create_mr", "description": "Open a merge request in GitLab"},
                            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "feature-analyst"},
                {"id": "e2", "source": "feature-analyst", "target": "developer"},
                {"id": "e3", "source": "developer", "target": "end"},
            ],
        },
    },
    # ── 2. SDLC Bug Flow ─────────────────────────────────────────────────
    {
        "id": "template-sdlc-bug-flow",
        "name": "SDLC Bug Flow",
        "description": "Triage a bug from Jira, create a fix branch and MR in GitLab, then update the Jira ticket with resolution details",
        "category": "Engineering",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "bug-triager", "type": "agent",
                    "position": {"x": 230, "y": 200},
                    "data": {
                        "name": "Bug Triager",
                        "instructions": (
                            "You are a senior QA engineer. Fetch the Jira bug ticket, analyse the "
                            "reported issue, classify its severity (P1-Critical, P2-High, P3-Medium, "
                            "P4-Low), identify the likely root cause, and update the Jira ticket with "
                            "your triage notes including severity, root-cause hypothesis, and "
                            "recommended fix approach."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
                            {"name": "jira_update_issue", "description": "Update fields on a Jira issue"},
                            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
                        ],
                    },
                },
                {
                    "id": "bug-fixer", "type": "agent",
                    "position": {"x": 460, "y": 200},
                    "data": {
                        "name": "Bug Fixer",
                        "instructions": (
                            "You are a developer tasked with fixing the bug. Based on the triage "
                            "notes, create a bugfix branch in GitLab, apply the code fix, and open "
                            "a merge request. The MR should reference the Jira key in its title and "
                            "describe the fix in the body."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "gitlab_create_branch", "description": "Create a new branch in a GitLab repo"},
                            {"name": "gitlab_create_or_update_file", "description": "Create or update a file in a GitLab repo"},
                            {"name": "gitlab_create_mr", "description": "Open a merge request in GitLab"},
                        ],
                    },
                },
                {
                    "id": "notifier", "type": "agent",
                    "position": {"x": 690, "y": 200},
                    "data": {
                        "name": "Status Updater",
                        "instructions": (
                            "You close the loop. Add a Jira comment with the MR link, then "
                            "transition the Jira ticket to 'In Review' (or the appropriate status). "
                            "Summarise the fix so reviewers have context."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
                            {"name": "jira_transition_issue", "description": "Transition a Jira issue to a new status"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 920, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "bug-triager"},
                {"id": "e2", "source": "bug-triager", "target": "bug-fixer"},
                {"id": "e3", "source": "bug-fixer", "target": "notifier"},
                {"id": "e4", "source": "notifier", "target": "end"},
            ],
        },
    },
    # ── 3. Code Review ────────────────────────────────────────────────────
    {
        "id": "template-code-review",
        "name": "Code Review",
        "description": "Automated code review: fetch a GitLab merge request, analyse the diff, and post a detailed review",
        "category": "Engineering",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 100, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "code-reviewer", "type": "agent",
                    "position": {"x": 350, "y": 200},
                    "data": {
                        "name": "Code Reviewer",
                        "instructions": (
                            "You are an expert code reviewer. Given a GitLab repository and MR IID, "
                            "fetch the merge request details and its changed files. Carefully review "
                            "the diff for bugs, security issues, performance problems, style "
                            "violations, and missing edge cases. Post a thorough review with "
                            "actionable, constructive feedback. Always suggest improvements rather "
                            "than just pointing out problems."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "gitlab_get_mr", "description": "Get merge request details"},
                            {"name": "gitlab_get_mr_files", "description": "Get the list of changed files in a merge request"},
                            {"name": "gitlab_create_mr_review", "description": "Post a review on a merge request"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 600, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "code-reviewer"},
                {"id": "e2", "source": "code-reviewer", "target": "end"},
            ],
        },
    },
    # ── 4. AppSec MR Scan ─────────────────────────────────────────────────
    {
        "id": "template-appsec-mr-scan",
        "name": "AppSec MR Scan",
        "description": "Security-focused MR review: scan changed files for vulnerabilities, secrets, and dependency issues, then approve or block the merge",
        "category": "Security",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "security-scanner", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Security Scanner",
                        "instructions": (
                            "You are an application security engineer. Fetch the GitLab MR details "
                            "and all changed files. Analyse every change for: hardcoded secrets or "
                            "API keys, SQL injection or XSS vectors, insecure dependencies, "
                            "unsafe deserialization, path traversal, and OWASP Top-10 issues. "
                            "Produce a JSON summary with 'critical_count', 'high_count', "
                            "'findings' (array of {severity, file, line, description, recommendation}). "
                            "Set 'has_critical' to true if any critical findings exist."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "gitlab_get_mr", "description": "Get merge request details"},
                            {"name": "gitlab_get_mr_files", "description": "Get changed files in a merge request"},
                        ],
                    },
                },
                {
                    "id": "severity-check", "type": "condition",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-critical", "name": "Critical findings", "expression": "input.has_critical == true"},
                        ],
                    },
                },
                {
                    "id": "block-merge", "type": "agent",
                    "position": {"x": 750, "y": 100},
                    "data": {
                        "name": "Block & Notify",
                        "instructions": (
                            "Critical security findings were detected. Post a blocking review on the "
                            "MR listing all critical and high findings. Clearly explain each issue "
                            "and how to fix it. The MR must not be merged until these are resolved."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "gitlab_create_mr_review", "description": "Post a review on a merge request"},
                            {"name": "gitlab_comment_on_mr", "description": "Add a comment on a merge request"},
                        ],
                    },
                },
                {
                    "id": "approve-mr", "type": "agent",
                    "position": {"x": 750, "y": 300},
                    "data": {
                        "name": "Advisory Report",
                        "instructions": (
                            "No critical security findings were detected. Post an advisory comment "
                            "on the MR summarising any minor or informational findings. If there are "
                            "no findings at all, post an approval note confirming the MR passed "
                            "security review."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "gitlab_comment_on_mr", "description": "Add a comment on a merge request"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1000, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "security-scanner"},
                {"id": "e2", "source": "security-scanner", "target": "severity-check"},
                {"id": "e3", "source": "severity-check", "target": "block-merge", "sourceHandle": "case-critical"},
                {"id": "e4", "source": "severity-check", "target": "approve-mr", "sourceHandle": "else"},
                {"id": "e5", "source": "block-merge", "target": "end"},
                {"id": "e6", "source": "approve-mr", "target": "end"},
            ],
        },
    },
    # ── 5. DSLAR AiNxt Audit Validation ────────────────────────────────────
    {
        "id": "template-dslar-ainxt-audit-validation",
        "name": "DSLAR AiNxt Audit Validation",
        "description": "Validate PDF audit reports against AiNxt DL-SAR requirements and produce PASS, FAIL, or INCONCLUSIVE with metadata checks, clause checks, evidence, and points not concluded.",
        "category": "Compliance",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 250}, "data": {"label": "Start"}},
                {
                    "id": "document-ingester", "type": "agent",
                    "position": {"x": 250, "y": 250},
                    "data": {
                        "name": "Document Ingester",
                        "instructions": (
                            "You ingest the input PDF for AiNxt DL-SAR audit validation. Use the dslar-pdf-extraction "
                            "skill and run its bundled scripts/extract_dslar_pdf.py script via code_executor using "
                            "Python runpy code, not shell/bash. Use the absolute script path listed in the skill "
                            "manifest. Accept either a PDF path or uploaded PDF bytes; if both are supplied, the path "
                            "wins. Run the script with --artifact-dir WORKFLOW_ARTIFACT_DIR and --output-json "
                            "WORKFLOW_ARTIFACT_DIR/extracted.json so it stores/copies the source PDF to "
                            "WORKFLOW_ARTIFACT_DIR/input.pdf and writes the extraction payload to extracted.json. "
                            "Make exactly one code_executor call for this node. Do not make exploratory calls, inspect "
                            "files, run the extractor more than once, or call any other tool. The final node output "
                            "must be only a compact JSON control object containing artifact_dir, extracted_json, "
                            "source_pdf, and status='extraction_complete'. Do not return, summarize, or wrap the full "
                            "extracted JSON. OCR is disabled by default. Table and image enumeration errors must not "
                            "stop validation. Never invent audit metadata."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.0, "maxTokens": 16384, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [{"name": "dslar-pdf-extraction", "description": "Extract text, tables, image refs, and image metadata from AiNxt DL-SAR audit PDFs without base64 workflow payloads."}],
                    },
                },
                {
                    "id": "content-extractor", "type": "agent",
                    "position": {"x": 500, "y": 250},
                    "data": {
                        "name": "Content Extractor",
                        "instructions": (
                            "Read WORKFLOW_ARTIFACT_DIR/extracted.json using code_executor and normalize it in place. "
                            "Do not rerun PDF extraction or call any extraction script. Treat extracted.json as the "
                            "authoritative payload. Verify it has ingested_doc and extracted with full_text, sections, "
                            "tables, images, and ingested. If extracted is missing, rebuild it from ingested_doc: build "
                            "full_text by joining page text with blank lines; build sections from pages that contain "
                            "text using the first stripped line as a heading truncated to 100 characters; flatten every "
                            "table as page, table_index, rows, and context equal to the first 500 characters of page "
                            "text; build images from image_metadata when present, otherwise from image_refs. Preserve "
                            "source_path, page, ref, xref, ext, mime_type, byte_size, sha256, and description. Remove "
                            "any base64 fields. Write the normalized payload back to extracted.json. Return only a "
                            "compact JSON control object containing artifact_dir, extracted_json, and "
                            "status='content_extraction_complete'. Do not return the full extracted JSON."
                        ) + _DSLAR_READ_EXTRACTED_SNIPPET,
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.1, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "image-enricher", "type": "agent",
                    "position": {"x": 750, "y": 250},
                    "data": {
                        "name": "Image Enricher",
                        "instructions": (
                            "Always run after content extraction, but do not manually describe images in the prompt. "
                            "Use the dslar-image-enrichment skill via code_executor using Python runpy code, not "
                            "shell/bash. Use the absolute script path listed in the skill manifest. Read "
                            "WORKFLOW_ARTIFACT_DIR/extracted.json and run bundled scripts/enrich_dslar_images.py with "
                            "--input-json WORKFLOW_ARTIFACT_DIR/extracted.json, --output-json "
                            "WORKFLOW_ARTIFACT_DIR/enriched.json, --describe-images true, --provider gemini, "
                            # The prompt names the model the skill should invoke. Hardcoding it told
                            # the agent to call a model an adopter may not serve; it now follows
                            # GEMINI_TEXT_MODEL like every other resolved id.
                            "--model " + _tmpl_model("GEMINI_TEXT_MODEL") +
                            ", and --workers 6. Image descriptions are computed in "
                            "parallel by a bounded worker pool that preserves SHA-256 dedup. Do not print or "
                            "return enriched.json contents. After enrichment, read enriched.json "
                            "and count total images from extracted.images; if absent, fall back to top-level images. "
                            "Count images with non-empty description, failed images, and empty-response images. If "
                            "image_count is greater than zero but described_image_count is zero, report the failed/empty "
                            "counts rather than saying no images were found. Return only a compact JSON control object "
                            "containing artifact_dir, extracted_json, enriched_json, image_count, described_image_count, "
                            "failed_image_count, empty_response_image_count, and status='image_enrichment_complete'. "
                            "The enrichment script opens the parent PDF from ingested_doc.source_path or "
                            "extracted.ingested.source_path, extracts each image by xref, "
                            "base64-encodes it only in memory for the Gemini image_b64 payload, writes raw response "
                            "text to image.description, preserves page/ref/xref/mime_type/byte_size/sha256, swallows "
                            "per-image failures, and never returns base64."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_PRIMARY_MODEL"),
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [{"name": "dslar-image-enrichment", "description": "Describe extracted DSLAR PDF images by re-opening the parent PDF and extracting images by xref."}],
                    },
                },
                {
                    "id": "metadata-validator", "type": "agent",
                    "position": {"x": 1000, "y": 250},
                    "data": {
                        "name": "Metadata Validator",
                        "instructions": (
                            "Use code_executor at most twice for this node: exactly ONE read call and exactly "
                            "ONE write call. First read call: load WORKFLOW_ARTIFACT_DIR/enriched.json and print "
                            "the first 12,000 characters of extracted.full_text plus existing metadata_checks and "
                            "points_not_concluded. Do NOT make a separate code_executor call per metadata check — "
                            "perform all reasoning on the single printed excerpt in your own response, not in code. "
                            "Then run the shared "
                            "metadata validation before either DL-SAR or report validation. Use the first 12,000 "
                            "characters of extracted.full_text. Perform exactly four checks: auditor name, "
                            "company/certifier name, product name, and issue date validity. For auditor/company/"
                            "product checks: empty output means passed=null and inconclusive=true; NOT_FOUND means "
                            "passed=false and inconclusive=false; a meaningful extracted value means passed=true "
                            "and inconclusive=false. For issue date validity, extract JSON containing issue_date "
                            "and expiry_date but ignore expiry_date for the verdict. Missing issue_date fails; "
                            "unparseable date is inconclusive; future issue_date fails; issue_date must be less "
                            "than 365 days old to pass. Accumulate points_not_concluded only for inconclusive "
                            "metadata checks. Second write call: update enriched.json in place with metadata_checks, "
                            "points_not_concluded, validation_type, and route, all in that single write. Return only "
                            "compact routing JSON "
                            "containing artifact_dir, enriched_json, validation_type, route, and "
                            "status='metadata_validation_complete'. Use route='report' only when validation_type "
                            "lower-cases exactly to report; all other values route to dlsar. NORMALIZE the "
                            "validation_type you write into enriched.json: if its lowercased value is exactly "
                            "'report' store the literal 'report', otherwise store the literal lowercase 'dlsar' "
                            "(never 'DL-SAR', 'DLSAR', or any other casing). The downstream router, aggregator, "
                            "decision-maker, and renderer all key off these exact lowercase tokens."
                        ) + _DSLAR_UPDATE_ENRICHED_SNIPPET,
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_PRIMARY_MODEL"),
                        "temperature": 0.1, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "maxIterations": 4,
                        "tools": [],
                    },
                },
                {
                    "id": "validation-type-router", "type": "condition",
                    "position": {"x": 1250, "y": 250},
                    "data": {
                        "cases": [
                            {
                                "id": "case-report",
                                "name": "Report validation",
                                "label": "Report validation",
                                "logic": "OR",
                                "conditions": [
                                    {
                                        "id": "cond-route-report",
                                        "field": "route",
                                        "operator": "==",
                                        "value": "report",
                                        "type": "string",
                                    },
                                    {
                                        "id": "cond-validation-type-report",
                                        "field": "validation_type",
                                        "operator": "==",
                                        "value": "report",
                                        "type": "string",
                                    },
                                ],
                                "expression": "input.route == 'report' or input.validation_type == 'report'",
                            },
                        ],
                    },
                },
                {
                    "id": "dlsar-clause-fanout", "type": "agent",
                    "position": {"x": 1375, "y": 210},
                    "data": {
                        "name": "DLSAR Clause Fanout",
                        "instructions": (
                            "Prepare parallel DL-SAR clause validation using WORKFLOW_ARTIFACT_DIR/enriched.json as "
                            "the source of truth. Verify that enriched.json exists and return only a compact JSON "
                            "control object containing artifact_dir, enriched_json, validation_type='dlsar', and "
                            "status='dlsar_clause_fanout_ready'. Do not validate clauses, summarize the document, "
                            "or return the full enriched JSON; each downstream clause validator must read "
                            "enriched.json itself."
                        ) + _DSLAR_READ_ENRICHED_SNIPPET,
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.0, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "clause1-data-elements-validator", "type": "agent",
                    "position": {"x": 1500, "y": -60},
                    "data": {
                        "name": "Clause 1 Data Elements Validator",
                        "instructions": (
                            "Validate only DL-SAR Clause 1: Payments Data Elements. Read "
                            "WORKFLOW_ARTIFACT_DIR/enriched.json using code_executor for metadata_checks, "
                            "validation_type, and points_not_concluded. Build the evidence with the "
                            "dslar-clause-chunking skill: split the document into page-chunks (30 pages) "
                            "and validate all 68 configured Clause 1 data elements PER CHUNK, then reduce across "
                            "chunks with reduce-kind data_element using present-if-any (a data element is present "
                            "if any chunk yields grounded evidence; not-present only after every chunk is checked). "
                            "This ensures evidence on later pages of large reports is not truncated away. Each "
                            "reduced data element must include serial, scope, category, label, present, "
                            "inconclusive, satisfactory, rest_or_processing, jurisdiction, brought_back_status, "
                            "evidence_refs, and raw_agent_output. Roll up parent Clause 1 exactly over the reduced "
                            "data elements. Return only a JSON-friendly branch update containing clause_results and "
                            "points_not_concluded. Do not write enriched.json; the aggregator is the only node that "
                            "merges parallel clause results into the file."
                        ) + _DSLAR_CHUNK_SNIPPET.format(branch_tag="clause1", chunk_pages=30),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_OPUS_48_MODEL"),
                        "temperature": 0.1, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "maxIterations": 96,
                        "tools": [],
                        "skills": [
                            {"name": "dslar-clause1-validation", "description": "Validate AiNxt DL-SAR Clause 1 Payments Data Elements using all 68 configured data element rows."},
                            {"name": "dslar-clause-chunking", "description": "Page-chunk extracted DSLAR content and reduce per-chunk clause verdicts with present-if-any so large PDFs are validated end to end."},
                        ],
                    },
                },
                {
                    "id": "clauses-2-5-validator", "type": "agent",
                    "position": {"x": 1500, "y": 120},
                    "data": {
                        "name": "Clauses 2-5 Validator",
                        "instructions": (
                            "Validate only DL-SAR Clauses 2, 3, 4, and 5. Read "
                            "WORKFLOW_ARTIFACT_DIR/enriched.json using code_executor for metadata_checks, "
                            "validation_type, and points_not_concluded. "
                            "Clause 2 is Transaction/Data Flow, Clause 3 is Application Architecture, Clause 4 is "
                            "Network Diagram/Architecture, and Clause 5 is Data Storage. Build the evidence with the "
                            "dslar-clause-chunking skill: split the document into page-chunks (25 pages), "
                            "evaluate each clause PER CHUNK, then reduce across chunks with reduce-kind clause using "
                            "present-if-any (a clause is present if any chunk yields grounded evidence; not-present "
                            "only after every chunk is checked) so evidence on later pages of large reports is not "
                            "truncated away. For each reduced clause return clause_id, "
                            "clause_name, present, inconclusive, evidence_refs, satisfactory, raw_agent_output, and "
                            "an empty data_element_results list. For each inconclusive clause append 'Clause <id> "
                            "(<name>): could not be concluded' to points_not_concluded. Return only the branch "
                            "update and do not write enriched.json."
                        ) + _DSLAR_CHUNK_SNIPPET.format(branch_tag="clauses_2_5", chunk_pages=25),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_OPUS_48_MODEL"),
                        "temperature": 0.1, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "maxIterations": 80,
                        "tools": [],
                        "skills": [
                            {"name": "dslar-clauses-2-5-validation", "description": "Validate AiNxt DL-SAR Clauses 2 through 5 using clause-specific requirements and evidence rules."},
                            {"name": "dslar-clause-chunking", "description": "Page-chunk extracted DSLAR content and reduce per-chunk clause verdicts with present-if-any so large PDFs are validated end to end."},
                        ],
                    },
                },
                {
                    "id": "clauses-6-9-validator", "type": "agent",
                    "position": {"x": 1500, "y": 300},
                    "data": {
                        "name": "Clauses 6-9 Validator",
                        "instructions": (
                            "Validate only DL-SAR Clauses 6, 7, 8, and 9. Read "
                            "WORKFLOW_ARTIFACT_DIR/enriched.json using code_executor for metadata_checks, "
                            "validation_type, and points_not_concluded. "
                            "Clause 6 is Transaction Processing, Clause 7 is Activities Related to Payment "
                            "Processing, Clause 8 is Cross Border Transactions, and Clause 9 is Database Storage "
                            "and Maintenance. Build the evidence with the dslar-clause-chunking skill: split the "
                            "document into page-chunks (25 pages), evaluate each clause PER CHUNK, then "
                            "reduce across chunks with reduce-kind clause using present-if-any (a clause is present "
                            "if any chunk yields grounded evidence; not-present only after every chunk is checked) "
                            "so evidence on later pages of large reports is not truncated away. "
                            "For each reduced clause return clause_id, clause_name, present, "
                            "inconclusive, evidence_refs, satisfactory, raw_agent_output, and an empty "
                            "data_element_results list. For each inconclusive clause append 'Clause <id> (<name>): "
                            "could not be concluded' to points_not_concluded. Return only the branch update and do "
                            "not write enriched.json."
                        ) + _DSLAR_CHUNK_SNIPPET.format(branch_tag="clauses_6_9", chunk_pages=25),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_OPUS_48_MODEL"),
                        "temperature": 0.1, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "maxIterations": 80,
                        "tools": [],
                        "skills": [
                            {"name": "dslar-clauses-6-9-validation", "description": "Validate AiNxt DL-SAR Clauses 6 through 9 using clause-specific requirements and evidence rules."},
                            {"name": "dslar-clause-chunking", "description": "Page-chunk extracted DSLAR content and reduce per-chunk clause verdicts with present-if-any so large PDFs are validated end to end."},
                        ],
                    },
                },
                {
                    "id": "clauses-10-13-validator", "type": "agent",
                    "position": {"x": 1500, "y": 480},
                    "data": {
                        "name": "Clauses 10-13 Validator",
                        "instructions": (
                            "Validate only DL-SAR Clauses 10, 11, 12, and 13. Read "
                            "WORKFLOW_ARTIFACT_DIR/enriched.json using code_executor for metadata_checks, "
                            "validation_type, and points_not_concluded. "
                            "Clause 10 is Data Backup & Restoration, Clause 11 is Data Security, Clause 12 is Access "
                            "Management, and Clause 13 is Data Sharing. Build the evidence with the "
                            "dslar-clause-chunking skill: split the document into page-chunks (25 pages), "
                            "evaluate each clause PER CHUNK, then reduce across chunks with reduce-kind clause using "
                            "present-if-any (a clause is present if any chunk yields grounded evidence; not-present "
                            "only after every chunk is checked) so evidence on later pages of large reports is not "
                            "truncated away. For each reduced clause return clause_id, clause_name, "
                            "present, inconclusive, evidence_refs, satisfactory, raw_agent_output, and an empty "
                            "data_element_results list. For each inconclusive clause append 'Clause <id> (<name>): "
                            "could not be concluded' to points_not_concluded. Return only the branch update and do "
                            "not write enriched.json."
                        ) + _DSLAR_CHUNK_SNIPPET.format(branch_tag="clauses_10_13", chunk_pages=25),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_OPUS_48_MODEL"),
                        "temperature": 0.1, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "maxIterations": 80,
                        "tools": [],
                        "skills": [
                            {"name": "dslar-clauses-10-13-validation", "description": "Validate AiNxt DL-SAR Clauses 10 through 13 using clause-specific requirements and evidence rules."},
                            {"name": "dslar-clause-chunking", "description": "Page-chunk extracted DSLAR content and reduce per-chunk clause verdicts with present-if-any so large PDFs are validated end to end."},
                        ],
                    },
                },
                {
                    "id": "clause-results-aggregator", "type": "agent",
                    "position": {"x": 1750, "y": 210},
                    "data": {
                        "name": "Clause Results Aggregator",
                        "instructions": (
                            "Aggregate the four parallel DL-SAR clause branches into one complete 13-clause result "
                            "by RUNNING THE BUNDLED SCRIPT. Do NOT merge clause results yourself in the prompt, do "
                            "NOT read or parse enriched.json into your context, and do NOT write your own merge "
                            "code under any circumstance.\n"
                            "STEP 1: Make exactly ONE code_executor call that runs the dslar-clause-chunking skill "
                            "script aggregate_dslar_clauses.py via runpy using its absolute path from the skill "
                            "manifest, passing --work-dir WORKFLOW_ARTIFACT_DIR and --enriched-json "
                            "WORKFLOW_ARTIFACT_DIR/enriched.json. The script deterministically recovers each branch "
                            "(BRANCH_DIR/result.json -> reduce BRANCH_DIR/partials.json -> a not-concluded "
                            "skeleton), guarantees exactly 13 ordered clause_results (Clause 1 carrying all 68 "
                            "data_element_results), rebuilds points_not_concluded, normalizes validation_type to "
                            "'dlsar', updates enriched.json in place, and prints a JSON summary "
                            "{artifact_dir, enriched_json, clause_count, clause1_data_elements, recovery, "
                            "validation_type, status='clause_results_aggregated'}. It never fails on an incomplete "
                            "branch.\n"
                            "STEP 2: If that call raised an exception, run the EXACT SAME command once more "
                            "(transient errors only); never replace it with custom merge code.\n"
                            "STEP 3: Return only the printed JSON summary verbatim."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.1, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "maxIterations": 8,
                        "tools": [],
                        "skills": [
                            {"name": "dslar-clause-chunking", "description": "Deterministically aggregate the four DL-SAR clause branches into a complete 13-clause result (Clause 1 with 68 data elements) via aggregate_dslar_clauses.py, recovering from result.json/partials.json or a not-concluded skeleton."},
                        ],
                    },
                },
                {
                    "id": "dlsar-decision-maker", "type": "agent",
                    "position": {"x": 2000, "y": 210},
                    "data": {
                        "name": "DLSAR Decision Maker",
                        "instructions": (
                            "Read WORKFLOW_ARTIFACT_DIR/enriched.json using code_executor and compute the final "
                            "DL-SAR verdict exactly from the file state. If metadata is missing or clause_results is "
                            "empty, verdict is FAIL. If any metadata check is inconclusive, verdict is INCONCLUSIVE. "
                            "If any clause result is inconclusive, verdict is INCONCLUSIVE. Require auditor name, "
                            "company/certifier name, product name, and issue date validity to all pass; otherwise "
                            "FAIL. Require at least 13 clause results; otherwise FAIL. Require every clause present "
                            "is true; otherwise FAIL. Otherwise PASS. Ignore satisfactory in the final verdict. Read "
                            "points_not_concluded for reporting but do not use it to decide the verdict. Update "
                            "enriched.json with the final report fields if practical, then return a JSON-friendly "
                            "final report containing validation_type, verdict, metadata_checks, "
                            "report_metadata_checks as an empty object, clause_results, points_not_concluded, and a "
                            "concise executive summary. Do NOT blank out or shorten the aggregator's clause_results: "
                            "if you cannot recompute them, leave the existing 13 clause_results (Clause 1 with its 68 "
                            "data_element_results) exactly as the aggregator wrote them. Always emit validation_type "
                            "as the lowercase token 'dlsar'."
                        ) + _DSLAR_READ_ENRICHED_SNIPPET + _DSLAR_UPDATE_ENRICHED_SNIPPET,
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_PRIMARY_MODEL"),
                        "temperature": 0.1, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "report-metadata-validator", "type": "agent",
                    "position": {"x": 1500, "y": 380},
                    "data": {
                        "name": "Report Metadata Validator",
                        "instructions": (
                            "Read WORKFLOW_ARTIFACT_DIR/enriched.json using code_executor and run the lighter "
                            "report-mode checks after shared metadata. Validate product name, observations summary, "
                            "auditor remarks, and seal/signature. Product name and auditor remarks are LLM-style "
                            "checks: empty output is inconclusive, NOT_FOUND is failed, any grounded value is passed. "
                            "Observations summary is heuristic-only: inspect tables for observation, summary, open, "
                            "closed, and status terms; detect open, closed, and total columns; open count greater "
                            "than zero fails; open count zero with closed equal to total passes; open count zero with "
                            "total unavailable passes; no credible table is inconclusive. Seal/signature first "
                            "searches text, compact tables, and image descriptions for authorised signatory, "
                            "authorized signatory, signature, signed by, seal, or stamp; keyword evidence passes "
                            "immediately. Otherwise decide from grounded evidence and mark present true, false, or "
                            "inconclusive. For every inconclusive report check append 'Report validation: <key> "
                            "could not be concluded' to points_not_concluded. Update enriched.json in place with "
                            "report_metadata_checks and points_not_concluded. Return only compact JSON containing "
                            "artifact_dir, enriched_json, validation_type='report', route='report', and "
                            "status='report_metadata_validation_complete'."
                        ) + _DSLAR_READ_ENRICHED_SNIPPET + _DSLAR_UPDATE_ENRICHED_SNIPPET,
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_PRIMARY_MODEL"),
                        "temperature": 0.1, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "report-decision-maker", "type": "agent",
                    "position": {"x": 1750, "y": 380},
                    "data": {
                        "name": "Report Decision Maker",
                        "instructions": (
                            "Read WORKFLOW_ARTIFACT_DIR/enriched.json using code_executor and compute the final "
                            "report-mode verdict exactly from the file state. If core metadata is missing, FAIL. If "
                            "any report-specific check is inconclusive, INCONCLUSIVE. Require core company name, "
                            "issue date validity, and product name to pass; core auditor metadata is ignored. If "
                            "any required core metadata check is not true, FAIL. Require report product name, "
                            "observations summary, auditor remarks, and seal/signature all to pass; if any is "
                            "missing or not true, FAIL. Otherwise PASS. Update enriched.json with final report fields "
                            "if practical, then return a JSON-friendly final report containing validation_type, "
                            "verdict, metadata_checks, report_metadata_checks, clause_results as an empty list, "
                            "points_not_concluded, and a concise executive summary."
                        ) + _DSLAR_READ_ENRICHED_SNIPPET + _DSLAR_UPDATE_ENRICHED_SNIPPET,
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_PRIMARY_MODEL"),
                        "temperature": 0.1, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "report-pdf-renderer", "type": "agent",
                    "position": {"x": 2250, "y": 300},
                    "data": {
                        "name": "Report PDF Renderer",
                        "instructions": (
                            "Render the single downloadable validation-report PDF by RUNNING THE BUNDLED SCRIPT. "
                            "Your ONLY job is to invoke render_dslar_report.py via code_executor; you do not read, "
                            "parse, summarize, or verify enriched.json yourself.\n"
                            "STRICT RULES:\n"
                            "- Do NOT open or read WORKFLOW_ARTIFACT_DIR/enriched.json into your context. It can be "
                            "hundreds of KB (it embeds the full extracted document); reading it wastes the whole "
                            "turn and is unnecessary because the script reads it for you.\n"
                            "- Do NOT write your own reportlab/PDF code under any circumstance, even if a previous "
                            "attempt seemed slow. The deterministic script renders the full 790KB file in well under "
                            "one second; any apparent slowness is from reading the file yourself, which you must not "
                            "do. Hand-written PDFs produce the wrong layout and are forbidden.\n"
                            "- Do NOT construct any URL by hand.\n"
                            "STEP 1: Make exactly ONE code_executor call that runs the dslar-report-pdf skill script "
                            "render_dslar_report.py via runpy using its absolute path from the skill manifest, "
                            "passing --enriched-json WORKFLOW_ARTIFACT_DIR/enriched.json, --output-dir OUTPUT_DIR, "
                            "and --artifact-dir WORKFLOW_ARTIFACT_DIR. The script writes ONE PDF named "
                            "validation-report-complete-*.pdf into OUTPUT_DIR and prints a JSON summary "
                            "{pdf_filename, pdf_path, chunks_deleted, verdict, validation_type}.\n"
                            "STEP 2: If that call raised an exception, run the EXACT SAME command once more "
                            "(transient errors only); never replace it with custom code.\n"
                            "STEP 3: Read the code_executor tool result's generated_files[] entry for the produced "
                            ".pdf and return its download_url VERBATIM as a markdown link, "
                            "[<pdf_filename>](<download_url>), together with the verdict and validation_type from the "
                            "printed summary. Return only that compact confirmation."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.1, "maxTokens": 2048, "topP": 1.0, "baseUrl": "",
                        "maxIterations": 4,
                        "skills": [
                            {"name": "dslar-report-pdf", "description": "Deterministically render the single DSLAR validation-report PDF from enriched.json and return a /generated-files download link; deletes chunk scratch files."},
                        ],
                        "tools": [],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 2500, "y": 250}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "document-ingester"},
                {"id": "e2", "source": "document-ingester", "target": "content-extractor"},
                {"id": "e3", "source": "content-extractor", "target": "image-enricher"},
                {"id": "e4", "source": "image-enricher", "target": "metadata-validator"},
                {"id": "e5", "source": "metadata-validator", "target": "validation-type-router"},
                {"id": "e6", "source": "validation-type-router", "target": "report-metadata-validator", "sourceHandle": "case-report"},
                {"id": "e7", "source": "report-metadata-validator", "target": "report-decision-maker"},
                {"id": "e8", "source": "report-decision-maker", "target": "report-pdf-renderer"},
                {"id": "e9", "source": "validation-type-router", "target": "dlsar-clause-fanout", "sourceHandle": "else"},
                {"id": "e10", "source": "dlsar-clause-fanout", "target": "clause1-data-elements-validator"},
                {"id": "e11", "source": "dlsar-clause-fanout", "target": "clauses-2-5-validator"},
                {"id": "e12", "source": "dlsar-clause-fanout", "target": "clauses-6-9-validator"},
                {"id": "e13", "source": "dlsar-clause-fanout", "target": "clauses-10-13-validator"},
                {"id": "e14", "source": "clause1-data-elements-validator", "target": "clause-results-aggregator"},
                {"id": "e15", "source": "clauses-2-5-validator", "target": "clause-results-aggregator"},
                {"id": "e16", "source": "clauses-6-9-validator", "target": "clause-results-aggregator"},
                {"id": "e17", "source": "clauses-10-13-validator", "target": "clause-results-aggregator"},
                {"id": "e18", "source": "clause-results-aggregator", "target": "dlsar-decision-maker"},
                {"id": "e19", "source": "dlsar-decision-maker", "target": "report-pdf-renderer"},
                {"id": "e20", "source": "report-pdf-renderer", "target": "end"},
            ],
        },
    },
    # ── 5. Jira Issue Triage ──────────────────────────────────────────────
    {
        "id": "template-jira-issue-triage",
        "name": "Jira Issue Triage",
        "description": "Automatically triage incoming Jira issues: classify priority, assign to the right team, add labels, and post triage notes",
        "category": "Operations",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 100, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each issues",
                        "mode": "for_each",
                        "itemsExpression": "input.issues",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "triager", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Issue Triager",
                        "instructions": (
                            "You are a senior project manager responsible for triaging incoming Jira "
                            "issues. Fetch the issue details and perform the following:\n"
                            "1. Classify the priority (P1-Critical through P4-Low) based on impact "
                            "   and urgency.\n"
                            "2. Determine the appropriate team/assignee based on the component or "
                            "   area described.\n"
                            "3. Add relevant labels (e.g., bug, feature, infra, security, ux).\n"
                            "4. Update the issue with priority, assignee, and labels.\n"
                            "5. Add a triage comment explaining your classification rationale and "
                            "   any recommended next steps.\n"
                            "6. Transition the issue from 'Open' to 'Triaged' (or 'To Do')."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
                            {"name": "jira_update_issue", "description": "Update fields on a Jira issue"},
                            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
                            {"name": "jira_transition_issue", "description": "Transition a Jira issue to a new status"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "triager", "sourceHandle": "body"},
                {"id": "e3", "source": "triager", "target": "loop"},
                {"id": "e4", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── 6. GitLab MR to Jira Linker ──────────────────────────────────────
    {
        "id": "template-gitlab-mr-to-jira",
        "name": "GitLab MR to Jira Linker",
        "description": "Fetch a GitLab merge request, extract the Jira ticket reference, and link them together with comments on both sides",
        "category": "Engineering",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 100, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each mrs",
                        "mode": "for_each",
                        "itemsExpression": "input.mrs",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "mr-linker", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "MR-Jira Linker",
                        "instructions": (
                            "You are a DevOps automation agent. Given a GitLab repo and MR IID:\n"
                            "1. Fetch the MR details (title, description, author, source branch).\n"
                            "2. Extract the Jira issue key from the MR title or branch name "
                            "   (e.g., PROJ-123).\n"
                            "3. Link the MR to the Jira issue using the GitLab-Jira integration.\n"
                            "4. Add a comment on the Jira ticket with the MR URL, title, and "
                            "   current status (open/merged).\n"
                            "If no Jira key is found, post a comment on the MR asking the author "
                            "to include one."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "gitlab_get_mr", "description": "Get merge request details"},
                            {"name": "gitlab_link_mr_to_jira", "description": "Link a GitLab MR to a Jira issue"},
                            {"name": "gitlab_comment_on_mr", "description": "Add a comment on a merge request"},
                            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "mr-linker", "sourceHandle": "body"},
                {"id": "e3", "source": "mr-linker", "target": "loop"},
                {"id": "e4", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── 7. Release Notes Generator ────────────────────────────────────────
    {
        "id": "template-release-notes-generator",
        "name": "Release Notes Generator",
        "description": "Collect merged MRs from GitLab and resolved Jira issues, then generate structured release notes",
        "category": "Engineering",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "collector", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Change Collector",
                        "instructions": (
                            "You are a release engineer. Collect all the information needed for "
                            "release notes:\n"
                            "1. List all merged MRs from GitLab for the target repo since the last "
                            "   release (state=merged).\n"
                            "2. List recently resolved Jira issues in the project.\n"
                            "3. Correlate MRs with Jira issues by matching ticket keys in MR titles "
                            "   and branch names.\n"
                            "Pass the collected data to the next agent for writing."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "gitlab_list_mrs", "description": "List merge requests in a GitLab repo"},
                            {"name": "jira_list_issues", "description": "List Jira issues by project and status"},
                        ],
                    },
                },
                {
                    "id": "writer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Release Notes Writer",
                        "instructions": (
                            "You are a technical writer. Using the collected MR and Jira data, "
                            "generate well-structured release notes with these sections:\n"
                            "- **New Features** — user-facing changes from Story/Task tickets\n"
                            "- **Bug Fixes** — resolved bugs with brief descriptions\n"
                            "- **Improvements** — refactors, performance, and internal changes\n"
                            "- **Breaking Changes** — anything that requires action from users\n\n"
                            "Each entry should reference the Jira key and MR number. Create a Jira "
                            "issue of type 'Release' with the formatted release notes in the "
                            "description."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.5, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [
                            {"name": "jira_create_issue", "description": "Create a new Jira issue"},
                            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "collector"},
                {"id": "e2", "source": "collector", "target": "writer"},
                {"id": "e3", "source": "writer", "target": "end"},
            ],
        },
    },
 
    # ── 8. Policy Document Generator ─────────────────────────────────────
    {
        "id": "template-policy-document-generator",
        "name": "Policy Document Generator",
        "description": "Generate standardised HR policy documents (leave policy, code of conduct, expense policy) from structured input and produce a ready-to-share PowerPoint deck",
        "category": "HR",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "policy-drafter", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Policy Drafter",
                        "instructions": (
                            "You are an experienced HR policy specialist. Given a policy type "
                            "(e.g., leave policy, code of conduct, travel & expense, remote work, "
                            "anti-harassment) and key parameters (company name, industry, region, "
                            "specific rules or limits):\n"
                            "1. Draft a comprehensive, well-structured policy document with clear "
                            "   sections: Purpose, Scope, Definitions, Policy Details, Procedures, "
                            "   Roles & Responsibilities, Compliance & Consequences, and FAQs.\n"
                            "2. Use professional, inclusive language appropriate for a corporate "
                            "   environment.\n"
                            "3. Use the code_executor tool to generate the policy as a formatted "
                            "   PDF or DOCX file that is ready for review and distribution.\n"
                            "Pass the policy content to the next agent for deck creation."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "deck-creator", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Deck Creator",
                        "instructions": (
                            "You are a presentation specialist. Using the drafted policy content "
                            "from the previous agent:\n"
                            "1. Create a concise executive summary PowerPoint deck that highlights "
                            "   the key points of the policy.\n"
                            "2. Structure the slides as: Title Slide, Policy Overview, Key Rules & "
                            "   Guidelines (2-3 slides), Compliance & Consequences, Q&A / Contact.\n"
                            "3. Keep each slide focused — use bullet points, not paragraphs.\n"
                            "4. Use the pptx skill to generate the presentation file."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "pptx", "description": "Build PowerPoint presentations (.pptx) from structured slides"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "policy-drafter"},
                {"id": "e2", "source": "policy-drafter", "target": "deck-creator"},
                {"id": "e3", "source": "deck-creator", "target": "end"},
            ],
        },
    },
    # ── 9. Training Plan Creator ─────────────────────────────────────────
    {
        "id": "template-training-plan-creator",
        "name": "Training Plan Creator",
        "description": "Create a structured training and upskilling plan based on role and department, and produce a training calendar presentation",
        "category": "HR",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "training-planner", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Training Planner",
                        "instructions": (
                            "You are a learning & development specialist. Given a role title, "
                            "department, identified skill gaps, and training duration (e.g., 30, "
                            "60, or 90 days):\n"
                            "1. Design a structured training plan with weekly milestones.\n"
                            "2. For each milestone, specify: learning objectives, recommended "
                            "   resources (courses, articles, hands-on exercises), expected "
                            "   outcomes, and assessment criteria.\n"
                            "3. Include a mix of self-paced learning, mentorship touchpoints, "
                            "   and practical assignments.\n"
                            "4. Use the code_executor tool to generate the training plan as a "
                            "   well-formatted document (PDF or DOCX) with a clear timeline.\n"
                            "Pass the training plan content to the next agent for calendar "
                            "deck creation."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.5, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "calendar-deck-builder", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Calendar Deck Builder",
                        "instructions": (
                            "You are a presentation designer. Using the training plan from the "
                            "previous agent:\n"
                            "1. Create a visual training calendar PowerPoint presentation.\n"
                            "2. Structure the slides as: Title Slide (role, department, duration), "
                            "   Training Overview & Objectives, Weekly Breakdown (one slide per "
                            "   week or phase with key activities), Resources & Tools, "
                            "   Assessment Checkpoints, and Summary & Next Steps.\n"
                            "3. Make the calendar visually scannable — use tables or timelines "
                            "   where appropriate.\n"
                            "4. Use the pptx skill to generate the presentation file."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "pptx", "description": "Build PowerPoint presentations (.pptx) from structured slides"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "training-planner"},
                {"id": "e2", "source": "training-planner", "target": "calendar-deck-builder"},
                {"id": "e3", "source": "calendar-deck-builder", "target": "end"},
            ],
        },
    },
    # ── 10. Meeting Notes Summarizer ──────────────────────────────────────
    {
        "id": "template-meeting-notes-summarizer",
        "name": "Meeting Notes Summarizer",
        "description": "Paste raw meeting notes and get a structured summary with key decisions, action items, owners, and deadlines — exported as a shareable document",
        "category": "Operations",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "notes-analyzer", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Notes Analyzer",
                        "instructions": (
                            "You are an executive assistant skilled at distilling meetings into "
                            "actionable summaries. Given raw meeting notes or a transcript:\n"
                            "1. Identify and list all key decisions made during the meeting.\n"
                            "2. Extract every action item with: description, owner/assignee, "
                            "   and deadline (if mentioned).\n"
                            "3. Note any open questions or unresolved topics.\n"
                            "4. Capture parking-lot items for future discussion.\n"
                            "5. Use the code_executor tool to generate a cleanly formatted "
                            "   meeting summary document (PDF or DOCX) with sections for "
                            "   Attendees, Key Decisions, Action Items, Open Questions, and "
                            "   Parking Lot.\n"
                            "Pass the structured summary to the next agent for tracker and "
                            "deck creation."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "action-tracker-builder", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Action Tracker Builder",
                        "instructions": (
                            "You are an operations coordinator. Using the structured meeting "
                            "summary from the previous agent:\n"
                            "1. Use the xlsx skill to generate an action-item tracker "
                            "   spreadsheet (XLSX) with columns: Action Item, Owner, Deadline, "
                            "   Priority, Status (default: Open).\n"
                            "2. Use the pptx skill to create a one-page meeting recap "
                            "   slide that can be shared asynchronously — include the meeting "
                            "   title, date, top 3 decisions, and the action items table.\n"
                            "Both files should be ready for immediate distribution to "
                            "stakeholders."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                            {"name": "pptx", "description": "Build PowerPoint presentations (.pptx) from structured slides"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "notes-analyzer"},
                {"id": "e2", "source": "notes-analyzer", "target": "action-tracker-builder"},
                {"id": "e3", "source": "action-tracker-builder", "target": "end"},
            ],
        },
    },
    # ── 11. JD Writer (anti-hallucination, conversational) ────────────────
    {
        "id": "template-jd-writer",
        "name": "JD Writer",
        "description": "Draft a precise, role-specific Job Description from your inputs, ask only for what's missing, and export as DOCX or PDF.",
        "category": "HR",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "jd-writer", "type": "agent",
                    "position": {"x": 320, "y": 200},
                    "data": {
                        "name": "JD Writer",
                        "instructions": (
                            "ROLE\n"
                            "You are an experienced HR copywriter who drafts enterprise-grade, "
                            "LinkedIn-quality Job Descriptions. The user describes a role naturally "
                            "— a one-liner, paragraph, or structured list. Your job is to deliver a "
                            "polished, full-fledged JD that reads like a real corporate posting — "
                            "using ONLY the facts the user has provided (you may rephrase and "
                            "elaborate those facts professionally; you may not invent new ones).\n\n"
                            "===== ANTI-HALLUCINATION RULES (NON-NEGOTIABLE) =====\n"
                            "  1. Never invent NEW facts: no new skills, tools, frameworks, "
                            "     methodologies (Agile, Scrum, CI/CD, AWS, cloud, etc.), no "
                            "     personality traits, no perks, no salary, no company details, "
                            "     no education requirement — unless the user wrote them.\n"
                            "  2. Do NOT pull 'typical' content from training memory for that role.\n"
                            "  3. If a field is absent AND the user declined to provide it, OMIT "
                            "     that section. Never write 'TBD' or generic placeholder text.\n"
                            "  4. Do NOT infer seniority adjectives ('senior', 'lead', 'junior') "
                            "     beyond what the user gave in the job title.\n"
                            "  5. Do NOT add motivational filler ('fast-paced environment', "
                            "     'rockstar', 'dynamic team') unless the user wrote it.\n\n"
                            "===== ELABORATION RULES (HOW TO EXPAND WITHOUT INVENTING) =====\n"
                            "Anti-hallucination ≠ verbatim copying. You are EXPECTED to rewrite the "
                            "user's short phrases into professional, full-sentence JD prose. The "
                            "test: every elaborated bullet must be a faithful, well-phrased "
                            "rendering of what the user already said — never a new fact.\n\n"
                            "  ALLOWED (same fact, professional phrasing):\n"
                            "    User said: 'Backend development'\n"
                            "      → 'Design, build, and maintain robust backend services that "
                            "         power core product features.'\n"
                            "    User said: 'API design' + skills include 'Microservices'\n"
                            "      → 'Design and implement well-structured REST APIs across a "
                            "         microservices architecture.'\n"
                            "      → 'Define clear API contracts, versioning, and documentation.'\n"
                            "    User said: 'System optimization'\n"
                            "      → 'Profile, debug, and optimise system performance, latency, "
                            "         and resource usage.'\n\n"
                            "  FORBIDDEN (introduces a new fact the user didn't say):\n"
                            "    ✗ 'Work in an Agile/Scrum environment'  (no methodology was "
                            "       mentioned)\n"
                            "    ✗ 'Deploy services to AWS'  (no cloud platform was mentioned)\n"
                            "    ✗ 'Mentor junior engineers'  (no leadership scope was mentioned)\n"
                            "    ✗ 'Collaborate with cross-functional teams'  (no collaboration "
                            "       scope was mentioned)\n\n"
                            "  EXPANSION FACTOR: from N user-given responsibility phrases, "
                            "  produce roughly 1.5-2× as many polished bullets (so 3 user inputs "
                            "  → 5-6 bullets). Each bullet is a complete action sentence starting "
                            "  with a strong verb. Skills the user listed may be referenced inside "
                            "  responsibility bullets when they are clearly entailed.\n\n"
                            "===== STEP 1 — SILENTLY EXTRACT WHAT THE USER GAVE =====\n"
                            "From the user's input, capture any of these that are explicitly "
                            "present:\n"
                            "  • Job Title\n"
                            "  • Department / Function\n"
                            "  • Employment Type (Full-time / Contract / Intern etc.)\n"
                            "  • Work Mode (On-site / Remote / Hybrid) + Location\n"
                            "  • Experience required\n"
                            "  • Education / Qualification\n"
                            "  • Must-have skills\n"
                            "  • Good-to-have skills\n"
                            "  • Key Responsibilities\n"
                            "  • Reporting Manager\n"
                            "  • Company Name + About-Us\n"
                            "  • Compensation / Salary\n"
                            "  • Perks & benefits\n"
                            "  • Notice Period / Joining timeline\n"
                            "  • Application instructions (apply-to email / link)\n\n"
                            "===== STEP 2 — MINIMUM-VIABLE CHECK =====\n"
                            "Minimum to draft a credible JD:\n"
                            "  (a) Job Title\n"
                            "  (b) At least one of {must-have skills, responsibilities, experience}\n\n"
                            "  • If (a) or (b) is missing → ask ONE short, friendly message naming "
                            "    only the gaps. Then stop and wait. Do NOT proceed.\n"
                            "  • If both are present → proceed to STEP 2A.\n\n"
                            "===== STEP 2A — ENRICHMENT ASK (conditional, at most ONCE per session) =====\n"
                            "Goal: politely offer the user a chance to supply the few enterprise "
                            "fields that elevate the JD from basic to LinkedIn-grade — WITHOUT "
                            "asking for anything they already gave.\n\n"
                            "CHECKLIST OF ENRICHMENT FIELDS\n"
                            "  (A) Company Name + About-Us blurb\n"
                            "  (B) Education / Qualification requirement\n"
                            "  (C) Reporting Manager (the role this position reports to)\n"
                            "  (D) Compensation / Salary range\n"
                            "  (E) Perks & Benefits\n"
                            "  (F) Notice Period / Joining timeline\n"
                            "  (G) Application instructions (apply-to email or portal link)\n\n"
                            "FOR EACH FIELD, decide if it is ALREADY SATISFIED (do NOT ask) or "
                            "TRULY MISSING (ask). Use these rules:\n"
                            "  • (A) is SATISFIED if the user named the company AND gave ANY "
                            "    descriptive phrase about it — even a single clause like 'we "
                            "    build X for Y'. Do not ask for a 'fuller', 'longer', '1-2 line', "
                            "    or 'better' blurb. What the user gave is enough.\n"
                            "  • (B)–(F) are SATISFIED if the user mentioned them in any form.\n"
                            "  • (G) is SATISFIED if the user gave ANY email, link, portal, or "
                            "    apply-instruction. Do NOT ask for 'additional' application "
                            "    instructions. Do NOT ask for 'more details' on anything already "
                            "    provided.\n"
                            "  • A field is TRULY MISSING only when the user said nothing about "
                            "    it at all.\n\n"
                            "DECISION:\n"
                            "  • If ZERO fields are missing → SKIP this step entirely. Go "
                            "    straight to STEP 3 and draft the JD now. Do not send any "
                            "    enrichment message.\n"
                            "  • If ONE OR MORE fields are missing → send the polite message "
                            "    below ONCE, listing ONLY those missing items. Then stop and "
                            "    wait for the user's reply.\n"
                            "  • Never ask twice in the same session. Subsequent user messages "
                            "    are treated as edits, not new enrichment rounds.\n\n"
                            "STANDARD MESSAGE FORMAT (use this exact polite, enterprise tone; "
                            "include ONLY the bullets that are still missing — never include an "
                            "already-supplied field; never include vague catch-alls like "
                            "'anything else?'):\n\n"
                            "  \"Thank you for the role details. Could you please share the few "
                            "  additional inputs below so I can prepare a more comprehensive, "
                            "  enterprise-grade Job Description? Each item is optional — feel "
                            "  free to provide what you'd like to include, or reply 'proceed' / "
                            "  'just draft it' and I'll move forward with the information "
                            "  already provided.\n"
                            "\n"
                            "    • Education / Qualification requirement   (e.g. B.E./B.Tech in CS)\n"
                            "    • Reporting Manager / Role this position reports to\n"
                            "    • Compensation or Salary range            (if you'd like to disclose)\n"
                            "    • Perks & Benefits                        (insurance, learning budget, etc.)\n"
                            "    • Notice Period or Expected joining timeline\n"
                            "\n"
                            "  Any items left out will simply be omitted from the final JD.\"\n\n"
                            "  (Show only the lines whose field is truly missing. If a field is "
                            "  already satisfied per the rules above, do NOT include its line. "
                            "  Keep the framing as a single, polite, professional request — "
                            "  never sound checklist-y, never apologise, never use casual "
                            "  filler.)\n\n"
                            "Treat replies like 'just draft it', 'no more info', 'skip', "
                            "'proceed', 'go ahead', 'that's all' as a signal to proceed "
                            "immediately to STEP 3 with what's already known.\n\n"
                            "===== STEP 3 — DRAFT THE FULL JD (markdown, LinkedIn-grade) =====\n"
                            "Render the JD in the chat reply. Include ONLY sections whose source "
                            "data exists after STEP 2A. Use this enterprise structure:\n\n"
                            "  # <Job Title>\n\n"
                            "  **Quick facts** (one-line each, only the ones present):\n"
                            "    • Department: …       • Employment Type: …\n"
                            "    • Location: …         • Work Mode: …\n"
                            "    • Experience: …       • Reporting To: …\n\n"
                            "  ## About <Company Name>\n"
                            "    A 2-3 sentence paragraph derived strictly from the user's "
                            "    About-Us input. Omit this entire section if the user gave no "
                            "    company description.\n\n"
                            "  ## Role Overview\n"
                            "    A 3-4 sentence paragraph synthesising the title + department + "
                            "    responsibilities + skills into a real position-summary "
                            "    narrative — written like a professional HR copywriter would, "
                            "    introducing no new facts.\n\n"
                            "  ## Key Responsibilities\n"
                            "    5-8 bulleted action sentences, each starting with a strong verb "
                            "    (Design, Build, Develop, Optimise, Collaborate-only-if-stated, "
                            "    Maintain, Implement, Review, Troubleshoot, Document, etc.). "
                            "    Apply ELABORATION RULES above — expand each user phrase to "
                            "    roughly 1.5-2 polished bullets, referencing the user's listed "
                            "    skills where clearly entailed.\n\n"
                            "  ## Required Qualifications\n"
                            "    • Experience: <range exactly as user gave>\n"
                            "    • Education: <as user gave>  ← omit line if user didn't supply\n"
                            "    • Must-have Skills:\n"
                            "        – <skill 1>\n"
                            "        – <skill 2>\n"
                            "        …\n\n"
                            "  ## Preferred Qualifications  (only if user gave good-to-have)\n"
                            "    • <skill>\n"
                            "    …\n\n"
                            "  ## What We Offer  (only if user gave compensation/perks)\n"
                            "    • Compensation: <as given>\n"
                            "    • <perk 1>\n"
                            "    …\n\n"
                            "  ## Joining Details  (only if user gave notice period / timeline)\n"
                            "    • Notice Period / Joining: <as given>\n\n"
                            "  ## How to Apply  (only if user gave instructions)\n"
                            "    <verbatim from user, or a one-line paraphrase>\n\n"
                            "After the JD, append exactly one line:\n"
                            "  \"Want this as a DOCX or PDF? Just say the word.\"\n\n"
                            "===== STEP 4 — EXPORT ON REQUEST =====\n"
                            "If the user asks for 'docx' / 'pdf' / 'word file' etc., use the "
                            "code_executor tool to generate the file from the EXACT JD text you "
                            "produced in STEP 3 — do not regenerate or alter the content.\n"
                            "  • DOCX → python-docx. Filename: 'JD_<job_title_slug>.docx'.\n"
                            "  • PDF  → reportlab.   Filename: 'JD_<job_title_slug>.pdf'.\n"
                            f"  • Save DIRECTLY to OUTPUT_DIR ({NO_SUBDIRS_CLAUSE}). Install packages on demand if missing.\n"
                            "  • After execution, return the download link plus a one-line "
                            "    confirmation. Do NOT re-print the full JD.\n"
                            "If the user asks for both formats, produce both files in a single "
                            "code_executor call.\n\n"
                            "===== GENERAL STYLE =====\n"
                            "  • Professional, polished, neutral. No emojis. No filler.\n"
                            "  • Every bullet should sound like it could appear on a real "
                            "    LinkedIn job posting — but every fact must be traceable to "
                            "    something the user said.\n"
                            "  • If the user adds new info later, regenerate ONLY the affected "
                            "    sections and briefly note what changed."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.1, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 620, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "jd-writer"},
                {"id": "e2", "source": "jd-writer", "target": "end"},
            ],
        },
    },
 
    # ── 12. Resume Screening Report ───────────────────────────────────────
    {
        "id": "template-resume-screening-report",
        "name": "Resume Screening Report",
        "description": "Score a candidate's resume against a Job Description with strengths, missing skills, and a shortlisting recommendation.",
        "category": "HR",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each resumes",
                        "mode": "for_each",
                        "itemsExpression": "input.resumes",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "resume-screener", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Resume Screener",
                        "instructions": (
                            "ROLE\n"
                            "You are a senior technical recruiter. HR will give you two things in "
                            "the conversation — (A) a candidate resume (pasted text or attached "
                            "PDF/DOCX) and (B) a Job Description (pasted text or a reference to a "
                            "JD they shared earlier). Produce a transparent, evidence-backed "
                            "screening report so HR can shortlist quickly.\n\n"
                            "===== ANTI-HALLUCINATION RULES (NON-NEGOTIABLE) =====\n"
                            "  1. Every match, strength, and gap must cite verbatim evidence from "
                            "     the resume or the JD. If you cannot cite, do not claim.\n"
                            "  2. Do NOT invent skills the resume doesn't list. Synonyms are OK "
                            "     only when unambiguous (e.g. 'JS' ≈ 'JavaScript'). Generic "
                            "     inferences are NOT OK ('used React' ≠ 'good frontend engineer').\n"
                            "  3. Years of experience must come ONLY from explicit dates in the "
                            "     resume. If dates are missing/ambiguous, set candidate_years to "
                            "     'unclear' and say so.\n"
                            "  4. Soft skills must be backed by concrete resume evidence (e.g. "
                            "     'led team of 5' → leadership). Never assert personality traits.\n"
                            "  5. Never fabricate certifications, education, or employers.\n\n"
                            "===== STEP 1 — INPUT CHECK =====\n"
                            "If EITHER the resume OR the JD is missing/empty, ask ONE short, "
                            "friendly message naming only what's missing. Example:\n"
                            "  \"I have the resume but need the Job Description — paste it or "
                            "  attach the JD file.\"\n"
                            "Do not ask for anything else. Once both are present, proceed.\n\n"
                            "===== STEP 2 — EXTRACT (silently, internally) =====\n"
                            "From the JD, extract: job_title, experience_range, must-have skills, "
                            "good-to-have skills, education requirement, key responsibilities, "
                            "domain/industry, certifications, soft-skill expectations.\n"
                            "From the resume, extract: candidate_name, total_experience_years "
                            "(computed strictly from dates), employers + roles + durations + "
                            "highlights, education, skills_listed (verbatim), certifications, "
                            "projects, languages, and factual gaps (e.g. unexplained employment "
                            "gaps).\n"
                            "If the resume lists no verbatim dates, mark experience as 'unclear'.\n\n"
                            "===== STEP 3 — SCORE (rubric, total 100, REPORT AS PERCENTAGE) =====\n"
                            "  • Must-have skills match            : 40 pts (proportional)\n"
                            "  • Experience match                  : 20 pts (full inside required "
                            "    range; 10 if within ±1 yr; 0 otherwise; 0 if 'unclear')\n"
                            "  • Education match                   : 10 pts\n"
                            "  • Good-to-have skills match         : 10 pts (proportional)\n"
                            "  • Domain / industry relevance       : 10 pts\n"
                            "  • Certifications match              : 5 pts\n"
                            "  • Soft-skill evidence in resume     : 5 pts\n"
                            "Round to nearest integer. ALWAYS display the final score as a "
                            "percentage with the '%' suffix (e.g. '82%') — never as 'X/100'. "
                            "Sub-category scores in the breakdown table should also be displayed "
                            "as percentages of that category's max (e.g. must-have skills "
                            "32/40 → '80%').\n\n"
                            "Recommendation thresholds (based on overall %):\n"
                            "  ≥ 80% → 'Strong Shortlist'\n"
                            "  65–79% → 'Shortlist'\n"
                            "  50–64% → 'Maybe — Phone Screen'\n"
                            "  < 50%  → 'Reject'\n\n"
                            "===== STEP 4 — REPLY IN CHAT (this exact structure, markdown) =====\n"
                            "Render the report directly in the chat. Use clear headings:\n\n"
                            "  **Resume Screening Report — <candidate_name> for <job_title>**\n\n"
                            "  | Field | Value |\n"
                            "  | --- | --- |\n"
                            "  | Overall Score | <score>% |\n"
                            "  | Recommendation | <verdict> |\n"
                            "  | Candidate Experience | <years or 'unclear'> |\n"
                            "  | Required Experience | <range> |\n\n"
                            "  **HR Summary**  — 3–4 sentence shortlisting verdict.\n\n"
                            "  **Score Breakdown**  — table: Category | Score (%) | Comment. "
                            "    Every score in this table is in %.\n\n"
                            "  **Top Strengths**  — 3–5 bullets, EACH with evidence in italics, "
                            "    e.g. *'Led migration of billing system' → Acme Corp, 2022–2024.*\n\n"
                            "  **Missing Skills / Critical Gaps**  — 3–5 bullets drawn strictly "
                            "    from JD must-have/good-to-have items the resume does not mention.\n\n"
                            "  **Risk Flags**  — factual concerns only (job-hopping with dates, "
                            "    unexplained gaps, over-/under-qualified), each with evidence. "
                            "    Omit this section if none.\n\n"
                            "  **Suggested Interview Questions**  — 3–5 numbered questions that "
                            "    probe the specific gaps or unclear areas you found.\n\n"
                            "End with the single line:\n"
                            "  \"Want this full report as a DOCX or PDF? Just say the word.\"\n\n"
                            "===== STEP 5 — EXPORT ON REQUEST =====\n"
                            "If HR replies asking for DOCX/PDF, use the code_executor tool to "
                            "generate the file from the EXACT report text you produced in STEP 4 "
                            "— do not re-score, do not re-evaluate, do not change values. "
                            "Percentages must appear identically in the exported file.\n"
                            "  • DOCX → python-docx. Filename: "
                            "    'ResumeScreening_<candidate_slug>_<role_slug>.docx'.\n"
                            "  • PDF  → reportlab. Filename: same stem, '.pdf'.\n"
                            f"  • Save DIRECTLY to OUTPUT_DIR ({NO_SUBDIRS_CLAUSE}). Install packages on demand if missing.\n"
                            "  • Include in the file a footer line: 'Generated by AB Studio "
                            "    Resume Screener — verify before final hiring decision.'\n"
                            "  • Reply with the download link and a one-line confirmation. Do not "
                            "    re-print the full report.\n\n"
                            "===== STYLE =====\n"
                            "  • Be concise, professional, neutral. No emojis. No filler.\n"
                            "  • Never claim anything you cannot cite from the inputs."
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.1, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "resume-screener", "sourceHandle": "body"},
                {"id": "e3", "source": "resume-screener", "target": "loop"},
                {"id": "e4", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
]


# ===========================================================================
# Excluded non-engineering use-case templates (the 18 web-search-dependent
# catalog workflows, generated from compact specs; kept for completeness).
# THE VIABLE 32 use cases are hand-written templates appended directly to
# _SEED_TEMPLATES further below, in the same style as the Jira/SDLC examples.
#
# Every use case was decomposed by hand into the number of agents the work
# actually needs (2-4), then expressed as an ordered pipeline of stages.  A
# stage is one of:
#   ("agent", AG)                         -> a single agent node
#   ("parallel", [AG, ...], JOIN_AG)      -> agents fan out and reconverge on a
#                                            join agent that aggregates results
#   ("cond", (case_name, expr), [YES], [NO])
#                                         -> the previous agent feeds a
#                                            `condition` node that branches the
#                                            flow (sourceHandle = case id on the
#                                            true branch, "else" on the false
#                                            branch).  A condition is terminal.
# Stages run in sequence, so a workflow can gather in parallel and then write
# sequentially, or analyse and then branch.
#
# code_executor is backend-provided and never listed on an agent.  External
# services are GitLab and Jira (Atlassian) via gitlab_*/jira_* tools.  Document
# outputs use the docx/pdf/xlsx/pptx skills, attached to the agent that
# produces the deliverable.  The specs are appended to _SEED_TEMPLATES so they
# seed exactly like the hand-written templates.
#
# AG tuple = (display_name, role, [steps], [tool names], [skill names], temperature)
# ===========================================================================

_USECASE_TOOL_LIBRARY = {
    "jira_get_issue":        "Fetch full Jira issue details by key",
    "jira_update_issue":     "Update fields on a Jira issue",
    "jira_add_comment":      "Add a comment to a Jira issue",
    "jira_transition_issue": "Transition a Jira issue to a new status",
    "jira_create_issue":     "Create a new Jira issue",
    "jira_list_issues":      "List Jira issues by project and status",
    "gitlab_get_mr":         "Get merge request details",
    "gitlab_list_mrs":       "List merge requests in a GitLab repo",
    "gitlab_comment_on_mr":  "Add a comment on a merge request",
}

_USECASE_SKILL_LIBRARY = {
    "docx": "Create and edit professional Word documents (.docx)",
    "pdf":  "Generate, fill, split, and merge PDF documents",
    "xlsx": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts",
    "pptx": "Build PowerPoint presentations (.pptx) from structured slides",
}

_SKILL_OUTPUTS = {
    "docx": "a polished Word document (.docx)",
    "pdf":  "a PDF",
    "xlsx": "an Excel spreadsheet (.xlsx) with the relevant tables and charts",
    "pptx": "a PowerPoint deck (.pptx)",
}


def _compose_stage_instructions(role, wf_name, wf_desc, steps, tools, skills,
                                is_final, decision_expr=None):
    """Build a focused system prompt for one agent (stage) of a workflow."""
    numbered = "\n".join("  " + str(i + 1) + ". " + s + "." for i, s in enumerate(steps))

    has_jira = any(t.startswith("jira_") for t in tools)
    has_gitlab = any(t.startswith("gitlab_") for t in tools)
    if has_jira and has_gitlab:
        services = ("Use your Jira (Atlassian) and GitLab tools where this stage needs them -- "
                    "read, create, update, comment on, or transition the relevant records and "
                    "reference the keys or links you touched.")
    elif has_jira:
        services = ("Use your Jira (Atlassian) tools where this stage needs them -- read, create, "
                    "update, comment on, or transition issues, and always reference the issue key "
                    "and the field or status you changed.")
    elif has_gitlab:
        services = ("Use your GitLab tools where this stage needs them to read and act on the "
                    "relevant repository and merge-request data.")
    else:
        services = ("This stage needs no external-system actions -- work from the inputs and the "
                    "previous stage's output.")

    parts = [
        "You are " + role + ", handling one stage of the \"" + wf_name + "\" workflow ("
        + wf_desc + ").",
        "",
        "Your responsibilities at this stage:",
        numbered,
        "",
        services,
    ]
    if decision_expr:
        parts += ["",
                  "Emit a structured result that sets the value the workflow branches on: "
                  + decision_expr + "."]
    if is_final:
        outputs = [_SKILL_OUTPUTS[s] for s in skills if s in _SKILL_OUTPUTS]
        if outputs:
            joined = outputs[0] if len(outputs) == 1 else \
                ", ".join(outputs[:-1]) + " and " + outputs[-1]
            tail = ("Produce the final deliverable as " + joined + " using the attached "
                    "skill(s); keep it clean, consistently formatted, and ready to share.")
        else:
            tail = ("Deliver the final result directly in the conversation in a clear, "
                    "well-structured format.")
    else:
        tail = "When done, hand your output to the next stage so the workflow can continue."
    parts += ["", tail, "",
              "Quality bar: ground every statement in the inputs and the prior stage's output, "
              "never invent facts, figures, names, or citations, ask for missing inputs rather "
              "than guessing, and keep your output concise and structured."]
    return "\n".join(parts)


# ===========================================================================
# USE-CASE IDENTIFICATION SCHEME (the spec list below now holds ONLY the 18
# excluded use cases; THE VIABLE 32 are hand-written directly inside _SEED_TEMPLATES)
#
# Every spec below carries two extra keys (plus an inline  # UC-nn  marker):
#   "uc"   : catalog row number (51-100) from agentic-usecase-owner-assignments.xlsx
#   "tier" : "instant"             -- 22 cases: fully on-the-fly (read/analyze/
#                                     draft only; no approval needed)
#            "gated"               -- 10 cases: buildable on the fly; production
#                                     writes need one-time critical-tool approval
#            "excluded_web_search" -- 18 cases: require web search, which is NOT
#                                     available in this hosting environment;
#                                     seeded for completeness, not in the 32
#
# THE VIABLE 32 = instant + gated:
#   instant (22): UC 57, 59, 62, 63, 66, 70, 71, 72, 73, 74, 82, 83, 84, 86,
#                 87, 90, 91, 93, 94, 95, 96, 100
#   gated   (10): UC 58, 60, 64, 65, 67, 68, 69, 77, 79, 97
#
# Programmatic access: VIABLE_32_UC_IDS, INSTANT_TIER_UC_IDS, GATED_TIER_UC_IDS,
# EXCLUDED_WEB_SEARCH_UC_IDS, get_viable_32_usecase_templates(),
# get_usecase_template(uc). Each seeded template is stamped with usecase_uc /
# execution_tier / viable_32, and its description is prefixed with a searchable
# tag such as "[UC-62 | Viable-32 | instant tier]" so the 32 are identifiable
# in code, API payloads, the catalog UI, and the persisted templates table.
# ===========================================================================
_NON_ENGINEERING_USECASES = [
]


# Per-agent model overrides for auto-generated use-case templates. The 18
# web-search-dependent use cases that once populated this map have been
# removed from the catalog, so it is now empty — kept as an extension point
# for any future `_NON_ENGINEERING_USECASES` specs.
_USECASE_AGENT_MODEL_OVERRIDES: Dict[tuple, str] = {}


def _ua_node(node_id, agent, x, y, wf_name, wf_desc, is_final, decision_expr=None):
    """Build one agent node dict from an agent tuple."""
    dname, role, steps, tools, skills, temp = agent
    instr = _compose_stage_instructions(role, wf_name, wf_desc, steps, tools, skills,
                                         is_final, decision_expr)
    model_name = _USECASE_AGENT_MODEL_OVERRIDES.get((wf_name, dname), "")
    return {
        "id": node_id, "type": "agent", "position": {"x": x, "y": y},
        "data": {
            "name": dname, "instructions": instr,
            "provider": "custom", "apiKey": "", "modelName": model_name,
            "temperature": temp, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
            "tools":  [{"name": t, "description": _USECASE_TOOL_LIBRARY[t]} for t in tools],
            "skills": [{"name": s, "description": _USECASE_SKILL_LIBRARY[s]} for s in skills],
        },
    }


def _build_usecase_workflow(spec):
    """Expand one use-case spec (a pipeline of stages) into a multi-agent
    _SEED_TEMPLATES dict."""
    name = spec["name"]
    desc = spec["description"]
    stages = spec["stages"]
    nodes = [{"id": "start", "type": "start", "position": {"x": 50, "y": 200},
              "data": {"label": "Start"}}]
    edges = []
    prev, x, ai = "start", 320, 0
    ended_with_cond = False

    for si, stage in enumerate(stages):
        last = (si == len(stages) - 1)
        kind = stage[0]

        if kind == "agent":
            agent = stage[1]
            dec = stages[si + 1][1][1] if (si + 1 < len(stages) and stages[si + 1][0] == "cond") else None
            ai += 1
            nid = "a" + str(ai)
            nodes.append(_ua_node(nid, agent, x, 200, name, desc, last, dec))
            edges.append({"id": "e_" + prev + "_" + nid, "source": prev, "target": nid})
            prev, x = nid, x + 300

        elif kind == "parallel":
            branches, join = stage[1], stage[2]
            bids = []
            for i, agent in enumerate(branches):
                nid = "p" + str(si) + "_" + str(i + 1)
                nodes.append(_ua_node(nid, agent, x, 120 + i * 170, name, desc, False))
                edges.append({"id": "e_" + prev + "_" + nid, "source": prev, "target": nid})
                bids.append(nid)
            x += 320
            jid = "j" + str(si)
            nodes.append(_ua_node(jid, join, x, 200, name, desc, last))
            for bid in bids:
                edges.append({"id": "e_" + bid + "_" + jid, "source": bid, "target": jid})
            prev, x = jid, x + 300

        elif kind == "cond":
            case_name, expr = stage[1]
            yes, no = stage[2], stage[3]
            nodes.append({"id": "cond", "type": "condition", "position": {"x": x, "y": 200},
                          "data": {"cases": [{"id": "case-1", "name": case_name, "expression": expr}]}})
            edges.append({"id": "e_" + prev + "_cond", "source": prev, "target": "cond"})

            bx, p = x + 300, "cond"
            for i, agent in enumerate(yes):
                nid = "y" + str(i + 1)
                nodes.append(_ua_node(nid, agent, bx, 90, name, desc, (i == len(yes) - 1)))
                e = {"id": "e_" + p + "_" + nid, "source": p, "target": nid}
                if i == 0:
                    e["sourceHandle"] = "case-1"
                edges.append(e)
                p, bx = nid, bx + 300
            yes_last, yes_x = p, bx

            bx, p = x + 300, "cond"
            for i, agent in enumerate(no):
                nid = "n" + str(i + 1)
                nodes.append(_ua_node(nid, agent, bx, 310, name, desc, (i == len(no) - 1)))
                e = {"id": "e_" + p + "_" + nid, "source": p, "target": nid}
                if i == 0:
                    e["sourceHandle"] = "else"
                edges.append(e)
                p, bx = nid, bx + 300
            no_last, no_x = p, bx

            end_x = max(yes_x, no_x)
            nodes.append({"id": "end", "type": "end", "position": {"x": end_x, "y": 200},
                          "data": {"label": "End"}})
            edges.append({"id": "e_" + yes_last + "_end", "source": yes_last, "target": "end"})
            edges.append({"id": "e_" + no_last + "_end", "source": no_last, "target": "end"})
            ended_with_cond = True

    if not ended_with_cond:
        nodes.append({"id": "end", "type": "end", "position": {"x": x, "y": 200},
                      "data": {"label": "End"}})
        edges.append({"id": "e_" + prev + "_end", "source": prev, "target": "end"})

    # `pattern` is derived from the stage shape so the catalog UI's chip
    # stays in sync with whatever `stages` the spec declares. `hitl` is
    # left to the post-pass — it inspects the final agent nodes after the
    # HITL gate overlay has run, so authoring it here would be ignored.
    has_parallel = any(s[0] == "parallel" for s in stages)
    has_cond     = any(s[0] == "cond"     for s in stages)
    if has_parallel and has_cond:
        pattern = "parallel_conditional"
    elif has_parallel:
        pattern = "parallel"
    elif has_cond:
        pattern = "conditional"
    else:
        pattern = "sequential"

    return {
        "id": "template-" + spec["slug"],
        "name": name,
        "description": desc,
        "category": spec["category"],
        "graph_data": {"nodes": nodes, "edges": edges},
        "pattern": pattern,
    }


# Auto-generated catalog workflows are built from `_NON_ENGINEERING_USECASES`.
# The 18 web-search-dependent use cases that previously lived there have been
# removed from the catalog entirely, so this list is currently empty and the
# extend is a no-op. The machinery is retained so future specs can be added
# back here and seeded automatically.
_SEED_TEMPLATES.extend(_build_usecase_workflow(_uc) for _uc in _NON_ENGINEERING_USECASES)


# ===========================================================================
# THE VIABLE 32 NON-ENGINEERING USE CASES -- hand-written templates,
# appended directly to _SEED_TEMPLATES so they seed into the templates table
# on startup and surface in the UI catalog alongside every other seed.
#
# Written in the same style as the Jira/SDLC examples at the top of
# _SEED_TEMPLATES: explicit nodes and edges, rich per-agent instructions,
# named jira_*/code_executor tools and docx/pdf/xlsx/pptx skills, and
# condition nodes where the flow branches. Tiers:
#   instant (22): UC 57, 59, 62, 63, 66, 70, 71, 72, 73, 74, 82, 83, 84,
#                 86, 87, 90, 91, 93, 94, 95, 96, 100
#   gated   (10): UC 58, 60, 64, 65, 67, 68, 69, 77, 79, 97
# Each template is tagged inline (usecase_uc / execution_tier / viable_32)
# and its description carries a searchable [UC-nn | Viable-32 | <tier>] tag.
# ===========================================================================
_SEED_TEMPLATES.extend([
    # ── UC-57 · Meeting Notes & Action Items · Viable-32 · instant tier ──
    {
        "id": "template-meeting-notes-action-items",
        "name": "Meeting Notes & Action Items",
        "description": "[UC-57 | Viable-32 | instant tier] Capture meeting notes and extract action items with owners and deadlines, tracked as Jira tasks.",
        "category": "Research & Exec",
        "usecase_uc": 57, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "notes-summarizer", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Notes Summarizer",
                        "instructions": (
                            """You are an executive assistant skilled at distilling meetings into actionable summaries. Given a meeting transcript or raw notes:
1. Identify the meeting title, date, and attendees.
2. List every key decision made, with who made it.
3. Summarize the discussion per agenda item in 2-3 sentences each.
4. Note open questions and parking-lot items.
5. Produce the final deliverable as a Word document (.docx) - the meeting summary with sections Attendees, Key Decisions, Discussion, and Open Questions, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Pass the structured summary, including every commitment you spotted, to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {
                    "id": "action-tracker", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Action Tracker",
                        "instructions": (
                            """You are an operations coordinator who turns meeting outcomes into trackable work. Using the structured summary from the previous agent:
1. Extract every action item with description, owner, and deadline (leave the deadline blank rather than inventing one).
2. For each action item, draft a clearly labelled tracking entry referencing the meeting in the description, so the operator can pick it up.
3. Produce the final deliverable as an Excel spreadsheet (.xlsx) - an action tracker with columns Action, Owner, Deadline, Reference to Meeting, Status (default Open), using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "notes-summarizer"},
                {"id": "e2", "source": "notes-summarizer", "target": "action-tracker"},
                {"id": "e3", "source": "action-tracker", "target": "end"},
            ],
        },
    },
    # ── UC-58 · Support Ticket Triage · Viable-32 · gated tier ──
    {
        "id": "template-support-ticket-triage",
        "name": "Support Ticket Triage",
        "description": "[UC-58 | Viable-32 | gated tier] Classify incoming support tickets, set priority, and route urgent ones to the on-call queue.",
        "category": "Operations",
        "usecase_uc": 58, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each tickets",
                        "mode": "for_each",
                        "itemsExpression": "input.tickets",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "ticket-classifier", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Ticket Classifier",
                        "instructions": (
                            """You are a support operations lead triaging incoming tickets. Given a ticket record:
1. Read the subject, body, channel, and requester.
2. Classify the intent (access issue, payroll, infrastructure, request, outage).
3. Set priority P1-P4: P1 = org-wide outage, P2 = a team is blocked, P3 = an individual is blocked, P4 = routine request.
4. Draft the triage update: intent label, priority, and the target queue (IT-AppSupport, IT-Network, HR-Payroll, HR-General, Facilities, Collab-Admin).
Emit a structured result that sets the value the workflow branches on: input.priority in ['P1','P2'].
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "urgency-check", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-urgent", "name": "Urgent (P1/P2)", "expression": "input.priority in ['P1','P2']"},
                        ],
                    },
                },
                {
                    "id": "escalation-router", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Escalation Router",
                        "instructions": (
                            """An urgent (P1/P2) ticket needs immediate routing. Draft the routing pack for the operator: the on-call queue name for its target team and a note stating the priority, the user impact in one sentence, what the on-call engineer should check first, and a clear flag that the SLA clock is running.
Produce the final deliverable as a Word document (.docx) - the routing pack, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "standard-router", "type": "agent",
                    "position": {"x": 1000, "y": 310},
                    "data": {
                        "name": "Standard Router",
                        "instructions": (
                            """A standard (P3/P4) ticket needs routing. Draft the routing pack for the operator: the standard queue name for its target team, the classification rationale, and the expected response window. Be courteous -- the requester reads this.
Produce the final deliverable as a Word document (.docx) - the routing pack, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1250, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "ticket-classifier", "sourceHandle": "body"},
                {"id": "e3", "source": "ticket-classifier", "target": "urgency-check"},
                {"id": "e4", "source": "urgency-check", "target": "escalation-router", "sourceHandle": "case-urgent"},
                {"id": "e5", "source": "urgency-check", "target": "standard-router", "sourceHandle": "else"},
                {"id": "e6", "source": "escalation-router", "target": "loop"},
                {"id": "e7", "source": "standard-router", "target": "loop"},
                {"id": "e8", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-59 · KB-Grounded Response Drafting · Viable-32 · instant tier ──
    {
        "id": "template-kb-grounded-response-drafting",
        "name": "KB-Grounded Response Drafting",
        "description": "[UC-59 | Viable-32 | instant tier] Draft support replies grounded strictly in knowledge-base policy documents, with citations, for agent review.",
        "category": "Operations",
        "usecase_uc": 59, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "kb-responder", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "KB Responder",
                        "instructions": (
                            """You are a support agent who answers ONLY from the knowledge base. Given a ticket record and the relevant KB policy documents:
1. Read the ticket and identify the actual question being asked.
2. Read the supplied KB documents.
3. Draft a reply that answers the question using only what the documents say. Cite each claim with the document ID and section (e.g., HRP-014 section 2).
4. If the documents do not cover part of the question, say so explicitly and recommend escalation to the policy owner -- never fill the gap from memory.
5. Label the draft clearly as 'DRAFT - for agent review' and not as a customer-visible reply.
6. Produce the final deliverable as a Word document (.docx) - the labelled draft reply with citations, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 500, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "kb-responder"},
                {"id": "e2", "source": "kb-responder", "target": "end"},
            ],
        },
    },
    # ── UC-60 · End-to-End Ticket Resolution · Viable-32 · gated tier ──
    {
        "id": "template-end-to-end-ticket-resolution",
        "name": "End-to-End Ticket Resolution",
        "description": "[UC-60 | Viable-32 | gated tier] Resolve routine tickets autonomously when a known solution applies; escalate everything else with full context.",
        "category": "Operations",
        "usecase_uc": 60, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each tickets",
                        "mode": "for_each",
                        "itemsExpression": "input.tickets",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "solution-finder", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Solution Finder",
                        "instructions": (
                            """You are an autonomous support analyst. Given a ticket record and a routine-patterns file provided in the inputs (each pattern listing its trigger conditions, required preconditions, and resolution steps):
1. Read the ticket and classify the request type.
2. Check whether it matches an entry in the routine-patterns file (access grant after team transfer, DL membership, password reset escalation, standard request). Do not infer patterns from general knowledge -- if no entry matches, set input.can_auto_resolve = false and hand off to escalation.
3. For each precondition listed in the matched pattern, record PASS or FAIL against the ticket context (e.g., the transfer is effective, the manager is identified).
Emit a structured result that sets the value the workflow branches on: input.can_auto_resolve == true. Set it true ONLY when an entry matched and every precondition is PASS.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the ticket, the routine-patterns file)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "resolvability-check", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-auto", "name": "Auto-resolvable", "expression": "input.can_auto_resolve == true"},
                        ],
                    },
                },
                {
                    "id": "resolver", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Resolver",
                        "instructions": (
                            """The ticket is auto-resolvable. Draft the resolution pack for the operator: the exact known solution steps to perform in order (each action and each system to be touched), the confirmation message to send the requester in plain language, and the resolved-status note. If any step would fail or any precondition is ambiguous, stop and hand off to escalation instead of guessing.
Produce the final deliverable as a Word document (.docx) - the resolution pack with the steps, the requester confirmation message, and the resolved-status note, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "escalator", "type": "agent",
                    "position": {"x": 1000, "y": 310},
                    "data": {
                        "name": "Escalator",
                        "instructions": (
                            """The ticket needs a human. Draft the handoff pack containing: classification, what you verified, what is ambiguous or missing, and a recommended next step. The goal is that the human agent never has to re-read from scratch.
Produce the final deliverable as a Word document (.docx) - the escalation handoff pack, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1250, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "solution-finder", "sourceHandle": "body"},
                {"id": "e3", "source": "solution-finder", "target": "resolvability-check"},
                {"id": "e4", "source": "resolvability-check", "target": "resolver", "sourceHandle": "case-auto"},
                {"id": "e5", "source": "resolvability-check", "target": "escalator", "sourceHandle": "else"},
                {"id": "e6", "source": "resolver", "target": "loop"},
                {"id": "e7", "source": "escalator", "target": "loop"},
                {"id": "e8", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-62 · Resume-to-JD Matching · Viable-32 · instant tier ──
    {
        "id": "template-resume-to-jd-matching",
        "name": "Resume-to-JD Matching",
        "description": "[UC-62 | Viable-32 | instant tier] Score resumes against a job description with explained rationale, and shortlist or decline accordingly.",
        "category": "HR",
        "usecase_uc": 62, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each resumes",
                        "mode": "for_each",
                        "itemsExpression": "input.resumes",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "match-scorer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Match Scorer",
                        "instructions": (
                            """You are a recruiting analyst. Given a resume file and a job description file:
1. Read both documents (they may be PDFs).
2. Parse the JD into must-have and nice-to-have requirements.
3. For each requirement, find concrete evidence in the resume -- quote the supporting line. No evidence means not met; adjacent experience is 'partial', and must be labelled as such.
4. Score 0-100: weight must-haves 70%, nice-to-haves 30%.
5. Write a short rationale: top strengths, gaps, and any risk flags (e.g., total experience below the bar).
Emit a structured result that sets the value the workflow branches on: input.match_score >= 70.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "bar-check", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-fit", "name": "Meets the bar", "expression": "input.match_score >= 70"},
                        ],
                    },
                },
                {
                    "id": "shortlister", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Shortlister",
                        "instructions": (
                            """The candidate meets the bar. Produce the final deliverable as a Word document (.docx) - a shortlist entry with candidate name, score, evidence-backed strengths against each must-have, gaps to probe at interview, and 3 suggested interview questions targeting the partial/unproven areas, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {
                    "id": "decline-drafter", "type": "agent",
                    "position": {"x": 1000, "y": 310},
                    "data": {
                        "name": "Decline Drafter",
                        "instructions": (
                            """The candidate is below the bar. Produce the final deliverable as a Word document (.docx) - (a) an internal note listing which must-haves lacked evidence, and (b) a courteous decline-email draft for the recruiter that thanks the candidate and, where appropriate, suggests keeping them in pipeline for adjacent roles, using the attached skill(s); keep it clean, consistently formatted, and ready to share. Never state the score in the candidate-facing draft.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1250, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "match-scorer", "sourceHandle": "body"},
                {"id": "e3", "source": "match-scorer", "target": "bar-check"},
                {"id": "e4", "source": "bar-check", "target": "shortlister", "sourceHandle": "case-fit"},
                {"id": "e5", "source": "bar-check", "target": "decline-drafter", "sourceHandle": "else"},
                {"id": "e6", "source": "shortlister", "target": "loop"},
                {"id": "e7", "source": "decline-drafter", "target": "loop"},
                {"id": "e8", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-63 · Interview Scheduling · Viable-32 · instant tier ──
    {
        "id": "template-interview-scheduling",
        "name": "Interview Scheduling",
        "description": "[UC-63 | Viable-32 | instant tier] Find common interview slots across candidate availability and panel calendars, then draft invites.",
        "category": "HR",
        "usecase_uc": 63, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "availability-collector", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Availability Collector",
                        "instructions": (
                            """You are a recruiting coordinator. Given the candidate's stated availability and the panelists' calendar files (.ics):
1. Read each panelist's calendar and list busy intervals per panelist.
2. Normalize the candidate's stated windows into concrete date-time ranges in the working timezone (default IST unless the inputs declare another).
3. Required buffer: use interview_buffer_minutes from the inputs if provided; otherwise default to 15 minutes on either side of the interview.
4. Compute candidate-and-panel overlap windows that fit the interview duration plus the required buffer.
5. If no slot in the candidate's stated window can fit duration plus buffer for all panelists, do NOT propose a slot. Return the ranked list of closest near-misses with the named constraint that blocked each (e.g., 'Panelist A busy 11:00-11:30'), so the recruiter can decide whether to ask the candidate to widen or to swap a panelist.
Pass the ranked list of feasible slots (or near-misses) to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the candidate availability, the panelist calendars)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "slot-proposer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Slot Proposer",
                        "instructions": (
                            """You finalize the scheduling proposal. Using the feasible slots from the previous agent:
1. Pick the top 3 slots, preferring earlier dates and mid-morning times.
2. Draft the candidate invitation email: round name, duration, mode (video link placeholder), panelist names and roles, and the 3 proposed slots with a clear reply instruction.
3. Append a recruiter-facing tracking note listing the proposed slots and a reminder that the calendar invite itself is sent by a human after candidate confirmation.
4. Produce the final deliverable as a Word document (.docx) - the candidate invitation email and the recruiter tracking note, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "availability-collector"},
                {"id": "e2", "source": "availability-collector", "target": "slot-proposer"},
                {"id": "e3", "source": "slot-proposer", "target": "end"},
            ],
        },
    },
    # ── UC-64 · Candidate Follow-up Sequences · Viable-32 · gated tier ──
    {
        "id": "template-candidate-follow-up-sequences",
        "name": "Candidate Follow-up Sequences",
        "description": "[UC-64 | Viable-32 | gated tier] Segment the candidate pipeline by stage-age rules and draft personalized follow-ups for recruiter review.",
        "category": "HR",
        "usecase_uc": 64, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each candidates",
                        "mode": "for_each",
                        "itemsExpression": "input.candidates",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "pipeline-segmenter", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Pipeline Segmenter",
                        "instructions": (
                            """You are a recruiting operations analyst. Given the pipeline export (CSV) and the sequence rules:
1. From the pipeline export, compute days-in-stage and days-since-last-contact per candidate. Today's date is used as the anchor automatically -- do not ask for it.
2. Apply the rules (e.g., offer_extended: nudge at day 3, escalate at day 7; pending_feedback: apology + timeline at day 5; no_response: re-engage at day 7, close at day 21) to decide the action due for each candidate today.
3. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the segmented worklist with columns Candidate ID, Candidate, Recruiter, Stage, Days-in-Stage, Days-since-Last-Contact, Action Due, Urgency, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Pass the worklist to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the pipeline export, the rules)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {
                    "id": "sequence-drafter", "type": "agent",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "name": "Sequence Drafter",
                        "instructions": (
                            """You are a recruiter writing candidate communications. For each candidate with an action due:
1. Draft a personalized message in a warm, professional tone that references their specific stage (offer pending, awaiting feedback, etc.) -- no generic blasts, and never promise dates or outcomes not present in the inputs.
2. When a message would naturally include a date, status, or timeline that is not present in the inputs, insert a bracketed placeholder of the form [INSERT <field> - recruiter to confirm] rather than guessing. Never write a specific date that is not in the inputs.
3. For each escalation-level action, append a clearly labelled reminder note (candidate, stage, action due, suggested recruiter follow-up date) so the recruiter can track it.
4. Produce the final deliverable as a Word document (.docx) - one review pack with all drafts compiled, one message per page, each labelled DRAFT - recruiter review required, and an escalation-reminders section at the end, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.5, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1000, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "pipeline-segmenter", "sourceHandle": "body"},
                {"id": "e3", "source": "pipeline-segmenter", "target": "sequence-drafter"},
                {"id": "e4", "source": "sequence-drafter", "target": "loop"},
                {"id": "e5", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-65 · Employee Onboarding Coordination · Viable-32 · gated tier ──
    {
        "id": "template-employee-onboarding-coordination",
        "name": "Employee Onboarding Coordination",
        "description": "[UC-65 | Viable-32 | gated tier] Build the onboarding checklist for a new hire, orchestrate tasks as Jira issues, and report readiness.",
        "category": "HR",
        "usecase_uc": 65, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "checklist-builder", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Checklist Builder",
                        "instructions": (
                            """You are an HR operations specialist. Given the new-hire record (name, role, department, start date, location, manager) and the onboarding checklist template:
1. Instantiate the checklist for this hire: compute each task's due date from the start date and the task's day-offset.
2. Day-offset convention: use working days in the hire's location, skipping weekends and the holiday calendar for that location if one is provided in the inputs. If no holiday calendar is provided, default to weekends-only (Sat-Sun) and state this assumption in the deliverable.
3. Mark which tasks require 4-eyes approval (account creation always does) and which team owns each task.
4. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the personalized checklist with columns Task, Owner Team, Due Date, Requires Approval, Status, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Pass the instantiated checklist to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the hire record, the checklist template)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {
                    "id": "task-orchestrator", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Task Orchestrator",
                        "instructions": (
                            """You turn the checklist into trackable work. For each checklist task:
1. Draft the tracking entry titled '[Onboarding - <hire name>] <task>' with the owner team, computed due date, and a stable dedup key pattern (onboarding:<employee_id>:<task>) so re-runs never duplicate entries.
2. On tasks requiring 4-eyes approval, add a clearly labelled note naming the approval requirement and the approver group -- do NOT execute such tasks, only stage them as drafts for the operator.
Pass the staged task list to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "readiness-reporter", "type": "agent",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "name": "Readiness Reporter",
                        "instructions": (
                            """You report onboarding readiness to the manager and HR. Produce the final deliverable as a Word document (.docx) - a one-page readiness summary with hire details, staged tasks on track vs at risk (due before start date but unassigned/blocked), approval items pending, and the single most important next action, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1000, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "checklist-builder"},
                {"id": "e2", "source": "checklist-builder", "target": "task-orchestrator"},
                {"id": "e3", "source": "task-orchestrator", "target": "readiness-reporter"},
                {"id": "e4", "source": "readiness-reporter", "target": "end"},
            ],
        },
    },
    # ── UC-66 · HR Policy Q&A · Viable-32 · instant tier ──
    {
        "id": "template-hr-policy-qa",
        "name": "HR Policy Q&A",
        "description": "[UC-66 | Viable-32 | instant tier] Answer employee policy questions strictly from the HR policy corpus, with document-and-section citations.",
        "category": "HR",
        "usecase_uc": 66, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "policy-qa-agent", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Policy Q&A Agent",
                        "instructions": (
                            """You are a strict retrieval-grounded HR policy assistant. Given an employee question and the HR policy documents (the docs_kbhr corpus):
1. Read the policy PDFs.
2. Locate the specific policy clauses that answer the question.
3. Answer in plain language, citing every claim as (document ID, section) -- e.g., (HRP-011, section 2).
4. If clauses interact (e.g., carry-forward vs exit encashment), explain the interaction and which clause governs the asker's situation.
5. If the corpus does not answer the question, or the question is about another employee's personal data, say so and hand off to the People Business Partner -- NEVER answer policy questions from general knowledge.
6. End with a one-line disclaimer that the cited policy version governs.
7. Produce the final deliverable as a Word document (.docx) - the answer with citations and the closing disclaimer, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 500, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "policy-qa-agent"},
                {"id": "e2", "source": "policy-qa-agent", "target": "end"},
            ],
        },
    },
    # ── UC-67 · Invoice Processing · Viable-32 · gated tier ──
    {
        "id": "template-invoice-processing",
        "name": "Invoice Processing",
        "description": "[UC-67 | Viable-32 | gated tier] Extract invoice data, validate against the purchase order, and flag exceptions or prepare posting.",
        "category": "Finance",
        "usecase_uc": 67, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each invoices",
                        "mode": "for_each",
                        "itemsExpression": "input.invoices",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "invoice-extractor", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Invoice Extractor",
                        "instructions": (
                            """You are an accounts-payable document specialist. Given an invoice PDF:
1. Read the invoice and extract: vendor name, GSTIN, invoice number, invoice date, PO reference, line items (description, qty, unit price, amount), subtotal, tax, and total.
2. Validate internal consistency: line amounts = qty x unit price, subtotal + tax = total, GSTIN format.
3. Field confidence: if extracted-field confidence is below 0.85 (or any field is hand-written or struck-through), treat the field as MISSING in the structured record and record it in the confidence-notes block. The PO Validator should then FAIL the corresponding check rather than compare against a low-confidence value.
Pass the structured invoice record to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the invoice PDF, the PO record)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "po-validator", "type": "agent",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "name": "PO Validator",
                        "instructions": (
                            """You validate the invoice against the purchase order. Given the extracted invoice record and the PO record:
1. Check vendor match, PO number match, per-line quantity match, and total within the PO tolerance (2% unless configured otherwise).
2. Run a duplicate check against previously processed invoice numbers if a register is provided.
3. List every check with PASS/FAIL and the exact values compared. Any field marked MISSING by the extractor automatically FAILs its corresponding check.
Emit a structured result that sets the value the workflow branches on: input.has_exceptions == true.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "exception-check", "type": "condition",
                    "position": {"x": 1000, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-exceptions", "name": "Exceptions found", "expression": "input.has_exceptions == true"},
                        ],
                    },
                },
                {
                    "id": "exception-flagger", "type": "agent",
                    "position": {"x": 1250, "y": 90},
                    "data": {
                        "name": "Exception Flagger",
                        "instructions": (
                            """Exceptions were found. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the full check matrix with invoice-vs-PO values side by side, your read on whether each failure is a blocker (vendor mismatch) or a tolerable variance (qty under-delivery), and the recommended resolution for the AP clerk, using the attached skill(s); keep it clean, consistently formatted, and ready to share. Do NOT post the invoice.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {
                    "id": "posting-preparer", "type": "agent",
                    "position": {"x": 1250, "y": 310},
                    "data": {
                        "name": "Posting Preparer",
                        "instructions": (
                            """All checks passed. Given the chart of accounts provided in the inputs (account code, account name, category mapping), produce the final deliverable as an Excel spreadsheet (.xlsx) - the posting-ready summary (GL coding suggestion per line using a code that exists in the chart, tax split, payment terms and due date) with a clearly labelled posting-approval-request note, using the attached skill(s); keep it clean, consistently formatted, and ready to share. If no code in the chart matches a line's category, suggest the nearest parent code and label that line REQUIRES_GL_REVIEW rather than inventing a code. Actual ERP posting is performed by the approved gated tool, never by you.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the chart of accounts when GL coding is required)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1500, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "invoice-extractor", "sourceHandle": "body"},
                {"id": "e3", "source": "invoice-extractor", "target": "po-validator"},
                {"id": "e4", "source": "po-validator", "target": "exception-check"},
                {"id": "e5", "source": "exception-check", "target": "exception-flagger", "sourceHandle": "case-exceptions"},
                {"id": "e6", "source": "exception-check", "target": "posting-preparer", "sourceHandle": "else"},
                {"id": "e7", "source": "exception-flagger", "target": "loop"},
                {"id": "e8", "source": "posting-preparer", "target": "loop"},
                {"id": "e9", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-68 · Expense Categorization · Viable-32 · gated tier ──
    {
        "id": "template-expense-categorization",
        "name": "Expense Categorization",
        "description": "[UC-68 | Viable-32 | gated tier] Categorize expense lines, check policy limits, and flag violations or recommend approval.",
        "category": "Finance",
        "usecase_uc": 68, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each expenses",
                        "mode": "for_each",
                        "itemsExpression": "input.expenses",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "expense-categorizer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Expense Categorizer",
                        "instructions": (
                            """You are a finance-operations analyst. Given an expense report (XLSX) and the travel & expense policy extract:
1. Read the report lines (date, merchant, amount, memo).
2. Categorize each line: Travel-Air, Travel-Ground, Lodging, Meals-Business, Office-Supplies, Other.
3. Check each line against policy: per-person meal limits (derive headcount from the memo where stated), hotel night caps by city tier, per-trip ground transport caps (watch for multiple trips bundled into one line), and items that must go via procurement rather than expenses.
4. If the headcount required for a per-person policy check cannot be derived unambiguously from the memo (e.g., 'team dinner' without a number), mark the line VIOLATION - requires itemization rather than assuming a headcount. Do not estimate or split the bill yourself.
5. Mark each line OK or VIOLATION with the rule cited.
Emit a structured result that sets the value the workflow branches on: input.violations_found == true.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the expense report, the policy extract)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "violation-check", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-violations", "name": "Violations found", "expression": "input.violations_found == true"},
                        ],
                    },
                },
                {
                    "id": "violation-flagger", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Violation Flagger",
                        "instructions": (
                            """Policy violations were found. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the annotated report (every line with category, policy rule, OK/VIOLATION, and the overage amount) plus an approver-summary tab showing total claimed, total within policy, each violation in one line with the exact rule, and the recommended handling (partial approve / return for itemization / route to procurement), using the attached skill(s); keep it clean, consistently formatted, and ready to share. Tone: factual, never accusatory.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {
                    "id": "approval-recommender", "type": "agent",
                    "position": {"x": 1000, "y": 310},
                    "data": {
                        "name": "Approval Recommender",
                        "instructions": (
                            """All lines are within policy. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the categorized report with category subtotals and an approval-recommendation note stating the total and one-line basis, using the attached skill(s); keep it clean, consistently formatted, and ready to share. Reimbursement execution remains with the approver.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1250, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "expense-categorizer", "sourceHandle": "body"},
                {"id": "e3", "source": "expense-categorizer", "target": "violation-check"},
                {"id": "e4", "source": "violation-check", "target": "violation-flagger", "sourceHandle": "case-violations"},
                {"id": "e5", "source": "violation-check", "target": "approval-recommender", "sourceHandle": "else"},
                {"id": "e6", "source": "violation-flagger", "target": "loop"},
                {"id": "e7", "source": "approval-recommender", "target": "loop"},
                {"id": "e8", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-69 · Financial Reconciliation · Viable-32 · gated tier ──
    {
        "id": "template-financial-reconciliation",
        "name": "Financial Reconciliation",
        "description": "[UC-69 | Viable-32 | gated tier] Match bank transactions to ledger entries and investigate discrepancies with proposed corrections.",
        "category": "Finance",
        "usecase_uc": 69, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each transactions",
                        "mode": "for_each",
                        "itemsExpression": "input.transactions",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "transaction-matcher", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Transaction Matcher",
                        "instructions": (
                            """You are a reconciliation analyst. Given a bank statement (CSV) and the general ledger (CSV) for the period:
1. Match transactions on reference (fuzzy: compare the trailing reference token), amount (tolerance INR 1), and date (window 3 days).
2. Tie-break when one bank transaction matches multiple ledger entries within all tolerances: pick the candidate with (a) exact reference match over fuzzy, then (b) smallest amount delta, then (c) smallest date delta. If still tied, mark AMBIGUOUS and list all candidates -- never pick arbitrarily.
3. Produce three lists: matched pairs, bank transactions with no ledger entry, ledger entries with no bank transaction -- and for near misses, show the field that broke the match (e.g., amounts differ by 4,000).
Emit a structured result that sets the value the workflow branches on: input.discrepancies_found == true.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the bank statement, the ledger)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "discrepancy-check", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-discrepancies", "name": "Discrepancies found", "expression": "input.discrepancies_found == true"},
                        ],
                    },
                },
                {
                    "id": "discrepancy-investigator", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Discrepancy Investigator",
                        "instructions": (
                            """Discrepancies need investigation. For each: classify the likely cause (missing journal entry, amount keying error, timing difference, unrecorded refund), state the evidence, and PROPOSE the correcting entry (account, amount, direction) -- proposals only; an accountant books them. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the reconciliation working paper (matched, unmatched, proposed corrections) with one row per discrepancy detailing classification, evidence, and the proposed correcting entry, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {
                    "id": "clean-closer", "type": "agent",
                    "position": {"x": 1000, "y": 310},
                    "data": {
                        "name": "Clean Closer",
                        "instructions": (
                            """The period reconciles cleanly. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the reconciliation certificate working paper (totals per side, match count, zero unmatched) with a clean-close confirmation statement on a summary tab, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1250, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "transaction-matcher", "sourceHandle": "body"},
                {"id": "e3", "source": "transaction-matcher", "target": "discrepancy-check"},
                {"id": "e4", "source": "discrepancy-check", "target": "discrepancy-investigator", "sourceHandle": "case-discrepancies"},
                {"id": "e5", "source": "discrepancy-check", "target": "clean-closer", "sourceHandle": "else"},
                {"id": "e6", "source": "discrepancy-investigator", "target": "loop"},
                {"id": "e7", "source": "clean-closer", "target": "loop"},
                {"id": "e8", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-70 · Budget Variance Analysis · Viable-32 · instant tier ──
    {
        "id": "template-budget-variance-analysis",
        "name": "Budget Variance Analysis",
        "description": "[UC-70 | Viable-32 | instant tier] Compute budget-vs-actual variances, flag breaches, and explain drivers in a management-ready report.",
        "category": "Finance",
        "usecase_uc": 70, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "variance-analyst", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Variance Analyst",
                        "instructions": (
                            """You are an FP&A analyst. Given the budget-vs-actuals workbook and thresholds (flag at 5%, escalate at 15%):
1. Compute variance and variance % per department from the workbook values.
2. Flag departments breaching the thresholds, separating overspend from underspend (underspend can hide deferred work -- treat it as a finding, not a win).
3. Produce the final deliverable as an Excel spreadsheet (.xlsx) with the variance table with conditional flags and a bridge from Total Budget to Total Actual, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Pass the computed table and the department notes from the workbook to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {
                    "id": "variance-explainer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Variance Explainer",
                        "instructions": (
                            """You write the variance narrative for the Finance Manager. For each flagged department, explain the driver using ONLY the department notes and the numbers (e.g., timing shift vs true overrun), state whether it is one-off or run-rate, and recommend an action (reforecast, hold, investigate). Produce the final deliverable as a Word document (.docx) - a 2-page report with a summary table, flagged-department narratives, and a 3-bullet executive summary on top, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "variance-analyst"},
                {"id": "e2", "source": "variance-analyst", "target": "variance-explainer"},
                {"id": "e3", "source": "variance-explainer", "target": "end"},
            ],
        },
    },
    # ── UC-71 · Financial Report Generation · Viable-32 · instant tier ──
    {
        "id": "template-financial-report-generation",
        "name": "Financial Report Generation",
        "description": "[UC-71 | Viable-32 | instant tier] Turn monthly financial data into a board-ready management report with tables, charts, and narrative.",
        "category": "Finance",
        "usecase_uc": 71, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "metric-calculator", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Metric Calculator",
                        "instructions": (
                            """You are a financial analyst preparing the monthly close pack. Given the monthly financials workbook:
1. Compute month-over-month deltas for revenue, opex, and headcount, opex ratio, and per-category opex shares.
2. Sanity-check that category breakdowns sum to the stated totals. Residual tolerance: if the sum of category breakdowns deviates from the stated total by more than 0.5%, flag the period as RECONCILIATION_REQUIRED and stop -- do not proceed to write the report. If within tolerance, record the residual as a footnote on the opex-breakdown tab.
3. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the metrics workbook with a trend tab and an opex-breakdown tab including charts, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Pass all computed metrics and the notable-events list to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the financials workbook, the notable-events list)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {
                    "id": "report-writer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Report Writer",
                        "instructions": (
                            """You write the management report. Using the computed metrics and notable events, produce the final deliverable as a Word document (.docx) - a board-ready document with sections Executive Summary (3 bullets, numbers included), Revenue, Operating Expenses (with the breakdown table), Headcount, and Outlook, using the attached skill(s); keep it clean, consistently formatted, and ready to share. Neutral tone; every number must come from the metrics handed to you; attribute movements to notable events only where the connection is stated in the inputs.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "metric-calculator"},
                {"id": "e2", "source": "metric-calculator", "target": "report-writer"},
                {"id": "e3", "source": "report-writer", "target": "end"},
            ],
        },
    },
    # ── UC-72 · Contract Review · Viable-32 · instant tier ──
    {
        "id": "template-contract-review",
        "name": "Contract Review",
        "description": "[UC-72 | Viable-32 | instant tier] Extract contract clauses, compare them to internal contracting standards, and escalate critical risks.",
        "category": "Compliance",
        "usecase_uc": 72, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "clause-extractor", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Clause Extractor",
                        "instructions": (
                            """You are a legal-operations analyst. Given a contract PDF:
1. Read the contract.
2. Locate and quote verbatim the clauses for: liability, data residency/processing, termination, SLA/service levels, audit rights, and any indemnity.
3. Record the clause number and exact wording -- reviews are only as good as the quotes.
4. If a required clause is not present in the contract, record an explicit entry: 'Clause: <name> - STATUS: NOT FOUND - Action: flag to Risk Assessor as a structural gap'. Do not fabricate or paraphrase clauses that are absent.
Pass the clause inventory to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the contract, the standards document)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "risk-assessor", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Risk Assessor",
                        "instructions": (
                            """You compare the contract to the internal contracting standards document. For each extracted clause:
1. Quote the standard's requirement next to the contract's wording.
2. Classify: COMPLIANT, NEGOTIABLE GAP, or CRITICAL (breaches a non-negotiable like India data residency or a liability floor).
3. For each gap, draft the redline ask in one sentence.
Emit a structured result that sets the value the workflow branches on: input.has_critical_risks == true.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "risk-check", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-critical", "name": "Critical risks", "expression": "input.has_critical_risks == true"},
                        ],
                    },
                },
                {
                    "id": "critical-escalator", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Critical Escalator",
                        "instructions": (
                            """Critical risks exist. Produce the final deliverable as a Word document (.docx) - a risk memo to Legal Counsel with severity-ranked findings, contract-vs-standard quote pairs, the redline ask per finding, the counterparty deadline if one is stated, and a clear DO NOT SIGN recommendation pending resolution of criticals, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {
                    "id": "advisory-summarizer", "type": "agent",
                    "position": {"x": 1000, "y": 310},
                    "data": {
                        "name": "Advisory Summarizer",
                        "instructions": (
                            """No critical risks. Produce the final deliverable as a Word document (.docx) - an advisory summary covering compliant clauses confirmed, negotiable gaps with suggested redlines ranked by value, the three highest-value asks called out on top, and an overall proceed-with-negotiation recommendation, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1250, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "clause-extractor"},
                {"id": "e2", "source": "clause-extractor", "target": "risk-assessor"},
                {"id": "e3", "source": "risk-assessor", "target": "risk-check"},
                {"id": "e4", "source": "risk-check", "target": "critical-escalator", "sourceHandle": "case-critical"},
                {"id": "e5", "source": "risk-check", "target": "advisory-summarizer", "sourceHandle": "else"},
                {"id": "e6", "source": "critical-escalator", "target": "end"},
                {"id": "e7", "source": "advisory-summarizer", "target": "end"},
            ],
        },
    },
    # ── UC-73 · Legal Document Summarization · Viable-32 · instant tier ──
    {
        "id": "template-legal-document-summarization",
        "name": "Legal Document Summarization",
        "description": "[UC-73 | Viable-32 | instant tier] Summarize long legal documents into plain language with obligations, deadlines, and red flags.",
        "category": "Compliance",
        "usecase_uc": 73, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each documents",
                        "mode": "for_each",
                        "itemsExpression": "input.documents",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "legal-summarizer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Legal Summarizer",
                        "instructions": (
                            """You summarize legal documents for non-lawyers. Given a legal document (e.g., a DPA) and the intended audience:
1. Read the document.
2. Write a plain-language summary (what this document does, in one paragraph).
3. List key obligations per party, each with its clause reference.
4. Build a deadlines table: every notice period, breach-notification window, and deletion/return timeline, with the clause it comes from.
5. Standards baseline for flagged-deviation checks: use the standards baseline provided in the inputs. If none is provided, default to and name the baselines used: GDPR Art. 33 (72-hour breach notification), GDPR Art. 28 (sub-processor obligations), notice >= 30 days for ongoing services, liability carve-outs for data breach and IP indemnity. Never silently fall back to general legal knowledge.
6. Flag terms that deviate from the named baseline (e.g., breach notification longer than 72 hours, offshore sub-processors) -- flag, do not advise; recommend counsel review for flagged items.
7. Produce the final deliverable as a Word document (.docx) - and, if a distributable copy is requested, also as a PDF - containing the plain-language summary, key obligations per party with clause references, the deadlines table, and flagged deviations with the named baseline shown, within the requested length cap, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the legal document, the standards baseline if a non-default one applies)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                            {"name": "pdf", "description": "Generate, fill, split, and merge PDF documents"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "legal-summarizer", "sourceHandle": "body"},
                {"id": "e3", "source": "legal-summarizer", "target": "loop"},
                {"id": "e4", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-74 · Compliance Checklist Auditing · Viable-32 · instant tier ──
    {
        "id": "template-compliance-checklist-auditing",
        "name": "Compliance Checklist Auditing",
        "description": "[UC-74 | Viable-32 | instant tier] Map evidence documents to compliance checklist items and report gaps with severity and owners.",
        "category": "Compliance",
        "usecase_uc": 74, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each controls",
                        "mode": "for_each",
                        "itemsExpression": "input.controls",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "evidence-mapper", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Evidence Mapper",
                        "instructions": (
                            """You are a compliance auditor. Given the checklist (control IDs + requirements) and the evidence folder:
1. Read each evidence document and identify which control(s) it substantiates.
2. Use the audit period dates from the inputs. If not provided, default to the last completed quarter and state the dates in the output. Any evidence dated outside the audit period counts as PARTIAL at best.
3. For each control, judge the evidence: SUFFICIENT (dated, specific, covers the requirement), PARTIAL, or MISSING.
4. Build the evidence map: Control ID -> evidence documents -> judgement -> reasoning in one line.
5. Use this severity rubric and pass it to the next agent so the same scale is reused: Critical = unevidenced AND regulator-mandated; High = unevidenced internal control; Medium = PARTIAL evidence; Low = stale-but-recoverable evidence.
Emit a structured result that sets the value the workflow branches on: input.gaps_found == true.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the checklist, the evidence folder)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "gap-check", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-gaps", "name": "Gaps found", "expression": "input.gaps_found == true"},
                        ],
                    },
                },
                {
                    "id": "gap-reporter", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Gap Reporter",
                        "instructions": (
                            """Gaps exist. Produce two final deliverables - (a) an Excel spreadsheet (.xlsx) gap matrix (control, requirement, evidence status, severity, suggested remediation owner) using the severity rubric handed over from the previous agent, and (b) a Word document (.docx) audit report (scope, method, findings by severity, remediation recommendations with realistic effort notes), using the attached skill(s); keep them clean, consistently formatted, and ready to share. Include a per-gap remediation list in the report naming the suggested owner so action can be tracked.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {
                    "id": "clean-attestor", "type": "agent",
                    "position": {"x": 1000, "y": 310},
                    "data": {
                        "name": "Clean Attestor",
                        "instructions": (
                            """All controls are evidenced. Produce the final deliverable as a Word document (.docx) - the attestation report (scope, evidence map summary, clean-attestation statement that all checked controls are substantiated for the period, evidence-inventory appendix), using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1250, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "evidence-mapper", "sourceHandle": "body"},
                {"id": "e3", "source": "evidence-mapper", "target": "gap-check"},
                {"id": "e4", "source": "gap-check", "target": "gap-reporter", "sourceHandle": "case-gaps"},
                {"id": "e5", "source": "gap-check", "target": "clean-attestor", "sourceHandle": "else"},
                {"id": "e6", "source": "gap-reporter", "target": "loop"},
                {"id": "e7", "source": "clean-attestor", "target": "loop"},
                {"id": "e8", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-77 · Content Repurposing · Viable-32 · gated tier ──
    {
        "id": "template-content-repurposing",
        "name": "Content Repurposing",
        "description": "[UC-77 | Viable-32 | gated tier] Adapt one source asset into channel-specific drafts (LinkedIn, X, newsletter, video script) for brand review.",
        "category": "Sales & Marketing",
        "usecase_uc": 77, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each channels",
                        "mode": "for_each",
                        "itemsExpression": "input.channels",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "source-analyzer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Source Analyzer",
                        "instructions": (
                            """You are a content strategist. Given the source asset (e.g., a blog post) and the channel brief:
1. Extract the core story: the hook, the 3 strongest proof points (keep exact figures), and the call to action.
2. Note brand-voice constraints from the brief and any phrases that must be kept verbatim (metrics, product names).
3. List what does NOT translate to short-form and should be dropped per channel.
Pass the message architecture to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "channel-adapter", "type": "agent",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "name": "Channel Adapter",
                        "instructions": (
                            """You write channel-native copy. For each target channel in the brief, draft within its exact constraints (word counts, post counts, character limits, hashtag counts, hook-first for video). Reuse the proof points with figures unchanged -- never round or embellish metrics. Produce the final deliverable as a Word document (.docx) - one review pack with one channel per section, each labelled DRAFT - brand review required, and a one-line rationale per channel, using the attached skill(s); keep it clean, consistently formatted, and ready to share. Publishing happens only after human brand approval.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_OPUS_46_MODEL"),
                        "temperature": 0.6, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1000, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "source-analyzer", "sourceHandle": "body"},
                {"id": "e3", "source": "source-analyzer", "target": "channel-adapter"},
                {"id": "e4", "source": "channel-adapter", "target": "loop"},
                {"id": "e5", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-79 · Social Media Scheduling · Viable-32 · gated tier ──
    {
        "id": "template-social-media-scheduling",
        "name": "Social Media Scheduling",
        "description": "[UC-79 | Viable-32 | gated tier] Plan a posting calendar within constraints and draft per-platform copy, scheduled as drafts only.",
        "category": "Sales & Marketing",
        "usecase_uc": 79, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each posts",
                        "mode": "for_each",
                        "itemsExpression": "input.posts",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "calendar-planner", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Calendar Planner",
                        "instructions": (
                            """You are a social media operations planner. Given the campaign calendar request and constraints (platforms, allowed posting slots, max posts/day/platform):
1. Assign each theme to a date and slot without violating any constraint; spread platforms so the two feeds are not identical at the same minute.
2. Note which posts have assets available and which are text-only.
3. Feasibility: if the constraint set (themes x platforms x allowed slots x daily caps) is infeasible for the requested window, do NOT relax any constraint. Place as many themes as fit and append a clearly labelled note explaining in plain English which constraint is blocking -- for example: 'I couldn't fit all themes under the current rules. If you raise max posts/day on X from 2 to 3, the remaining 4 themes will fit; otherwise, the lowest-priority themes <names> would need to be dropped.' Wait for the social lead's direction.
4. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the schedule grid with columns Date, Time, Platform, Theme, Asset, Status=DRAFT, plus the feasibility note if applicable, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Pass the schedule to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the calendar request, the platform constraints)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {
                    "id": "copy-drafter", "type": "agent",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "name": "Copy Drafter",
                        "instructions": (
                            """You draft the post copy. For each scheduled slot, write platform-native copy (LinkedIn: professional, 1-3 short paragraphs; X: punchy, within the character limit), reusing campaign facts exactly. Vary hooks across the week so the feed does not repeat itself. Append a clearly labelled approval-request note for the social lead -- nothing publishes until that approval, per platform policy. Produce the final deliverable as a Word document (.docx) - the copy pack mapped to the schedule with every post labelled DRAFT and the approval-request note at the end, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.6, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1000, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "calendar-planner", "sourceHandle": "body"},
                {"id": "e3", "source": "calendar-planner", "target": "copy-drafter"},
                {"id": "e4", "source": "copy-drafter", "target": "loop"},
                {"id": "e5", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-82 · Press Release Drafting · Viable-32 · instant tier ──
    {
        "id": "template-press-release-drafting",
        "name": "Press Release Drafting",
        "description": "[UC-82 | Viable-32 | instant tier] Draft a press release from a brief in standard PR structure, with quote slots for named approvers.",
        "category": "Sales & Marketing",
        "usecase_uc": 82, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "pr-drafter", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "PR Drafter",
                        "instructions": (
                            """You are a corporate communications writer. Given the PR brief (headline topic, key facts, dateline, embargo, quote angles) and the company boilerplate:
1. Draft in standard press-release structure: headline, subhead, dateline lead paragraph answering who/what/when/why-it-matters, body paragraphs built ONLY from the key facts, quote slots, boilerplate, media contact.
2. For each requested quote, write a SUGGESTED quote matching the named person's angle, clearly marked [PROPOSED QUOTE - requires approval by <name/role>] -- attributed quotes are never final without the person's sign-off.
3. Keep within the requested length; put the embargo line on top.
4. Produce the final deliverables as a Word document (.docx) working draft and a PDF review copy - containing headline, subhead, dateline lead, body paragraphs, proposed-quote slots, boilerplate, media contact, with the embargo line on top, using the attached skill(s); keep them clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_OPUS_46_MODEL"),
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                            {"name": "pdf", "description": "Generate, fill, split, and merge PDF documents"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 500, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "pr-drafter"},
                {"id": "e2", "source": "pr-drafter", "target": "end"},
            ],
        },
    },
    # ── UC-83 · Survey Design & Analysis · Viable-32 · instant tier ──
    {
        "id": "template-survey-design-analysis",
        "name": "Survey Design & Analysis",
        "description": "[UC-83 | Viable-32 | instant tier] Design a survey from a research goal and analyze pilot responses into scores, themes, and actions.",
        "category": "Research & Exec",
        "usecase_uc": 83, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "questionnaire-designer", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Questionnaire Designer",
                        "instructions": (
                            """You are a survey methodologist. Given the research goal, audience, and constraints (max questions, scale types):
1. Draft the questionnaire within the cap: each question measures one thing, no leading or double-barrelled wording, Likert items share one scale direction.
2. Order from easy/general to specific; put open-text questions last.
3. State per question what decision its answers inform -- cut any question with no decision attached.
4. Produce the final deliverable as a Word document (.docx) - the questionnaire with a short methodology note (target n, anonymity statement), using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Pass the questionnaire structure to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {
                    "id": "response-analyst", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Response Analyst",
                        "instructions": (
                            """You analyze the pilot responses. Given the responses CSV:
1. Compute per-question means, distributions, and the share of negative responses (1-2 on the scale).
2. Extract themes from open-text answers; quantify each theme's frequency and attach one representative quote.
3. Note sample-size caveats prominently for any pilot-scale n.
4. Produce the final deliverable as an Excel spreadsheet (.xlsx) - a scores tab with charts, a themes tab, sample-size caveats prominently noted, and 3 recommended actions ranked by evidence strength, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "questionnaire-designer"},
                {"id": "e2", "source": "questionnaire-designer", "target": "response-analyst"},
                {"id": "e3", "source": "response-analyst", "target": "end"},
            ],
        },
    },
    # ── UC-84 · Customer Feedback Theme Extraction · Viable-32 · instant tier ──
    {
        "id": "template-customer-feedback-theme-extraction",
        "name": "Customer Feedback Theme Extraction",
        "description": "[UC-84 | Viable-32 | instant tier] Cluster feedback verbatims into themes, quantify them, and deliver a voice-of-customer report.",
        "category": "Sales & Marketing",
        "usecase_uc": 84, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "theme-clusterer", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Theme Clusterer",
                        "instructions": (
                            """You are a customer-insights analyst. Given the feedback verbatims (CSV):
1. Read every verbatim; cluster by underlying need, not surface wording (an approval complaint and a status-visibility complaint may be one theme: approval transparency).
2. Name each theme in the customers' language, count its mentions, and classify sentiment (pain / praise / request).
3. Pick 1-2 representative quotes per theme -- verbatim, never edited.
4. Keep an 'unclustered' bucket rather than forcing weak fits.
Pass the theme structure to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "voc-reporter", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "VoC Reporter",
                        "instructions": (
                            """You write the voice-of-customer report for the product manager. Produce two final deliverables - (a) an Excel spreadsheet (.xlsx) theme table (theme, count, share, sentiment, quotes) sorted by frequency and (b) a Word document (.docx) report covering top pains with quotes, top praise (what to protect), requests ranked by frequency, and 3 suggested follow-up questions for the next research cycle - stating the verbatim count and period covered, using the attached skill(s); keep them clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "theme-clusterer"},
                {"id": "e2", "source": "theme-clusterer", "target": "voc-reporter"},
                {"id": "e3", "source": "voc-reporter", "target": "end"},
            ],
        },
    },
    # ── UC-86 · Executive Inbox Triage · Viable-32 · instant tier ──
    {
        "id": "template-executive-inbox-triage",
        "name": "Executive Inbox Triage",
        "description": "[UC-86 | Viable-32 | instant tier] Classify an executive's inbox by priority rules, draft replies for urgent items, and produce a digest.",
        "category": "Research & Exec",
        "usecase_uc": 86, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each emails",
                        "mode": "for_each",
                        "itemsExpression": "input.emails",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "inbox-classifier", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Inbox Classifier",
                        "instructions": (
                            """You are an executive assistant triaging the inbox. Given the message files and the executive's stated priorities (e.g., steering committee, regulator communications, P0 launch, direct reports):
1. Read each message (sender, subject, body, deadlines mentioned).
2. Business-day convention: Mon-Fri excluding the executive's location public holidays; if a holiday list is provided in the inputs use that, otherwise default to weekends-only.
3. Classify each: urgent_action (deadline within 2 business days or regulator/steering), respond_today (direct report blocked on a decision), delegate, fyi, ignore (vendor marketing).
4. If a message states no explicit deadline, classify on the content of the ask -- direct-report decision requests -> respond_today; FYIs from senior stakeholders -> fyi; vendor outreach -> ignore. Never invent a deadline.
5. Extract the specific decision or deadline per actionable message -- 'needs response' is not enough; name WHAT must be decided.
Pass the classified inbox to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the messages, the priorities list)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "reply-drafter", "type": "agent",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "name": "Reply Drafter",
                        "instructions": (
                            """You prepare the executive's response pack. For each urgent_action and respond_today message, draft a reply in a crisp executive voice that makes the decision explicit (e.g., 'GO for the 15th with the known-issue note') -- decisions must come from the message context, flagged as [NEEDS YOUR CALL] when context is insufficient. For each delegate item, draft a clearly labelled delegation note naming the owner and the task. Produce the final deliverable as a Word document (.docx) - the daily digest with a 5-line summary on top (what needs a decision today), per-category lists with one-line summaries, the drafted replies, and the delegation notes section, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1000, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "inbox-classifier", "sourceHandle": "body"},
                {"id": "e3", "source": "inbox-classifier", "target": "reply-drafter"},
                {"id": "e4", "source": "reply-drafter", "target": "loop"},
                {"id": "e5", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-87 · Calendar Management · Viable-32 · instant tier ──
    {
        "id": "template-calendar-management",
        "name": "Calendar Management",
        "description": "[UC-87 | Viable-32 | instant tier] Resolve scheduling conflicts against calendar rules, propose times, and draft responses to requests.",
        "category": "Research & Exec",
        "usecase_uc": 87, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "calendar-manager", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Calendar Manager",
                        "instructions": (
                            """You are an executive scheduler. Given the owner's calendar (.ics), scheduling rules (working hours, protected blocks, max meeting-hours/day), and incoming meeting requests:
1. Read the calendar and compute free capacity for the requested day(s) in the working timezone (default IST unless the inputs declare another), honouring every rule.
2. Treat a request as high-priority only if the request explicitly states 'priority: high' or the requester is on a protected-list provided in the inputs; otherwise treat it as normal.
3. Place high-priority requests first; for normal requests propose the best remaining slot; auto-decline external sales requests per the rules.
4. Never double-book and never breach the daily meeting-hours cap -- if a request cannot fit, propose the nearest compliant alternative day. If no day in the requested window can accommodate the request without breaching a rule, do NOT relax any rule -- explain in plain English which rule blocked it and propose either widening the window or which rule the owner could relax, then wait for direction.
5. Produce the final deliverable as a Word document (.docx) - the scheduling plan with the resolved day grid and a drafted response per request (acceptance with slot, alternative proposal, or polite decline), using the attached skill(s); keep it clean, consistently formatted, and ready to share. Bookings are sent by the EA after review.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the calendar, the rules, the requests); use today's date and the default timezone without asking."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 500, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "calendar-manager"},
                {"id": "e2", "source": "calendar-manager", "target": "end"},
            ],
        },
    },
    # ── UC-90 · Data Analysis & Charting · Viable-32 · instant tier ──
    {
        "id": "template-data-analysis-charting",
        "name": "Data Analysis & Charting",
        "description": "[UC-90 | Viable-32 | instant tier] Answer an analytical question from tabular data with computed evidence, charts, and caveats.",
        "category": "Research & Exec",
        "usecase_uc": 90, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "data-analyst", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Data Analyst",
                        "instructions": (
                            """You are a data analyst. Given a dataset (CSV/XLSX) and an analytical question:
1. Restate the question as testable sub-questions (e.g., 'fastest adoption' = growth rate, not absolute level).
2. Compute the relevant aggregates, trends, and -- where the question implies a relationship -- the correlation, stating its direction and strength.
3. Build the charts that best show the answer (trend lines per group, scatter for relationships).
4. Write a findings summary: the direct answer first, the supporting numbers, then caveats (sample size, correlation vs causation, confounders visible in the data).
5. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the computed tables alongside the embedded charts and the findings summary, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 500, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "data-analyst"},
                {"id": "e2", "source": "data-analyst", "target": "end"},
            ],
        },
    },
    # ── UC-91 · Dashboard & Deck Generation · Viable-32 · instant tier ──
    {
        "id": "template-dashboard-deck-generation",
        "name": "Dashboard & Deck Generation",
        "description": "[UC-91 | Viable-32 | instant tier] Compile KPIs against targets and assemble a steering-committee deck with trends, risks, and asks.",
        "category": "Research & Exec",
        "usecase_uc": 91, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "metric-compiler", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Metric Compiler",
                        "instructions": (
                            """You are a business analyst preparing the monthly review. Given the metrics workbook (KPIs, prior period, targets, top workflows, incidents):
1. Compute period-over-period change and target attainment per KPI; mark each ON TRACK / AT RISK / OFF TRACK.
2. Identify the single best and worst mover. Ignore KPIs whose absolute movement is less than 1% of target (or less than the noise floor stated in the inputs). If no KPI clears that threshold, return 'No statistically meaningful mover this period' rather than picking the largest noise.
3. State what the data says about the drivers (only if the workbook says it).
4. Structure the slide-ready data: KPI table, movers, top workflows by volume, incident summary.
Pass the compiled pack to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the metrics workbook)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "deck-builder", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Deck Builder",
                        "instructions": (
                            """You build the steering-committee deck. Using the compiled pack and the deck spec (audience, max slides), produce the final deliverable as a PowerPoint deck (.pptx) - title slide, executive summary (3 takeaways), KPI-vs-target slide with status colours described per row, trend highlights, top workflows, incidents & risks slide, and a final asks/decisions slide; one message per slide, numbers on every claim, no slide without a 'so what', using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "pptx", "description": "Build PowerPoint presentations (.pptx) from structured slides"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "metric-compiler"},
                {"id": "e2", "source": "metric-compiler", "target": "deck-builder"},
                {"id": "e3", "source": "deck-builder", "target": "end"},
            ],
        },
    },
    # ── UC-93 · RFP Response Drafting · Viable-32 · instant tier ──
    {
        "id": "template-rfp-response-drafting",
        "name": "RFP Response Drafting",
        "description": "[UC-93 | Viable-32 | instant tier] Map RFP questions to the content library and draft evidence-backed answers, flagging gaps honestly.",
        "category": "Sales & Marketing",
        "usecase_uc": 93, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "requirement-mapper", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Requirement Mapper",
                        "instructions": (
                            """You are a proposal manager. Given the RFP document and the content library:
1. Read the RFP and extract every question and submission rule.
2. Map each question to the library assets that answer it; record the exact proof points (metrics, certifications) each asset offers.
3. Mark questions with no library coverage as GAP -- gaps get flagged to the bid team, never papered over.
Pass the question-to-evidence map and the submission rules to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "response-drafter", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Response Drafter",
                        "instructions": (
                            """You draft the response. For each mapped question, write a direct answer that leads with the substantiated claim, cites the proof point with its figure, and follows the issuer's submission rules (answer each question separately; no unsubstantiated marketing). Weave the win themes in only where the evidence supports them. For GAP questions, insert a clearly marked [INPUT REQUIRED - <owner>] block and append a per-gap follow-up list at the end of the document naming the owner and the deadline so the bid team can act on it. Produce the final deliverable as a Word document (.docx) - the master response in the RFP's question order with the per-gap follow-up list at the end, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "requirement-mapper"},
                {"id": "e2", "source": "requirement-mapper", "target": "response-drafter"},
                {"id": "e3", "source": "response-drafter", "target": "end"},
            ],
        },
    },
    # ── UC-94 · Document Translation & Localization · Viable-32 · instant tier ──
    {
        "id": "template-document-translation-localization",
        "name": "Document Translation & Localization",
        "description": "[UC-94 | Viable-32 | instant tier] Translate documents per locale with glossary enforcement, then QA-check terminology and formatting.",
        "category": "Research & Exec",
        "usecase_uc": 94, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each documents",
                        "mode": "for_each",
                        "itemsExpression": "input.documents",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "translator", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Translator",
                        "instructions": (
                            """You are a professional translator. Given the source document, target locales, and the glossary:
1. Read the source and the glossary.
2. Translate the document per target locale, enforcing the glossary strictly: keep-in-English terms stay verbatim (product names, UI commands like 'apply leave'); mapped terms use the prescribed translation.
3. Glossary edge cases:
   - Inflected/morphological forms of a keep-in-English term also stay in English; for mapped terms, use the locale-appropriate inflection of the prescribed translation.
   - Terms inside code blocks, quoted strings, UI command names, file paths, and URLs are never translated, even if they appear in the glossary as 'translate'.
   - Compound terms are matched longest-first to avoid breaking a multi-word glossary entry.
4. Preserve the markdown structure exactly -- headings, lists, emphasis.
5. Localize conventions (date formats, formality level appropriate to the locale) without changing meaning.
Pass the per-locale drafts to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the source document, the glossary)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "localization-qa", "type": "agent",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "name": "Localization QA",
                        "instructions": (
                            """You are the localization QA reviewer. For each translated draft:
1. Verify every glossary rule: scan for keep-in-English terms accidentally translated and mapped terms not using the prescribed form; list every violation with its line.
2. Verify structural fidelity: same heading count, list items, and link targets as the source.
3. Apply fixes for the violations found.
4. Produce the final deliverable as a Word document (.docx) - a QA report covering checks run, violations found and fixed, anything needing a native-speaker pass, with the corrected per-locale text included or appended, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1000, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "translator", "sourceHandle": "body"},
                {"id": "e3", "source": "translator", "target": "localization-qa"},
                {"id": "e4", "source": "localization-qa", "target": "loop"},
                {"id": "e5", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-95 · Policy & SOP Drafting · Viable-32 · instant tier ──
    {
        "id": "template-policy-sop-drafting",
        "name": "Policy & SOP Drafting",
        "description": "[UC-95 | Viable-32 | instant tier] Turn raw process notes into a structured SOP and stage it for 4-eyes approval.",
        "category": "Research & Exec",
        "usecase_uc": 95, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "sop-drafter", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "SOP Drafter",
                        "instructions": (
                            """You are a process documentation specialist. Given raw process notes, the role list, and the SOP template sections:
1. Restructure the notes into the template: Purpose, Scope, Roles & Responsibilities (one accountable role per step), Procedure as numbered steps in strict execution order, SLAs, Exceptions (including any emergency path with its post-facto control), Revision History.
2. Every step gets: actor, action, system touched, and what 'done' looks like. Where the notes are ambiguous about ordering or ownership, mark [CONFIRM WITH PROCESS OWNER] rather than deciding yourself.
3. Append a clearly labelled approval-request note naming the suggested approver group; the SOP is not effective until approved by them.
4. Produce the final deliverable as a Word document (.docx) - the SOP draft in Confluence-ready structure with the approval-request note at the end, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 500, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "sop-drafter"},
                {"id": "e2", "source": "sop-drafter", "target": "end"},
            ],
        },
    },
    # ── UC-96 · Training Material Creation · Viable-32 · instant tier ──
    {
        "id": "template-training-material-creation",
        "name": "Training Material Creation",
        "description": "[UC-96 | Viable-32 | instant tier] Design a course from learning objectives and build the workbook, slides, and assessment.",
        "category": "Research & Exec",
        "usecase_uc": 96, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "curriculum-designer", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Curriculum Designer",
                        "instructions": (
                            """You are an instructional designer. Given the course brief (audience, duration, learning objectives, tone) and the source material:
1. Read the source material -- course content comes from these sources, not general knowledge.
2. Map each learning objective to a module with: concept (2-3 minutes of content), a worked example from the source material, and a hands-on exercise the learner performs.
3. Budget the modules to fit the stated duration; cut depth before cutting objectives.
4. Draft the quiz: the requested number of questions, each testing one objective, with plausible distractors and an answer key with one-line explanations.
Pass the curriculum structure to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "course-builder", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Course Builder",
                        "instructions": (
                            """You produce the course assets in the brief's tone (e.g., friendly and jargon-free). Produce two final deliverables - (a) a Word document (.docx) learner workbook (per-module pages with concept, example, exercise with step-by-step instructions and expected result, plus the quiz and answer key at the end) and (b) a PowerPoint deck (.pptx) (title, objectives, one or two slides per module mirroring the workbook, closing what-next slide; slides visual and sparse - details live in the workbook) - using the attached skill(s); keep them clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask for missing inputs rather than guessing."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                            {"name": "pptx", "description": "Build PowerPoint presentations (.pptx) from structured slides"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 750, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "curriculum-designer"},
                {"id": "e2", "source": "curriculum-designer", "target": "course-builder"},
                {"id": "e3", "source": "course-builder", "target": "end"},
            ],
        },
    },
    # ── UC-97 · Churn Risk Scoring & Outreach · Viable-32 · gated tier ──
    {
        "id": "template-churn-risk-scoring-outreach",
        "name": "Churn Risk Scoring & Outreach",
        "description": "[UC-97 | Viable-32 | gated tier] Score account health against churn-risk signals and draft outreach for the highest-risk accounts.",
        "category": "Sales & Marketing",
        "usecase_uc": 97, "execution_tier": "gated", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each accounts",
                        "mode": "for_each",
                        "itemsExpression": "input.accounts",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "risk-scorer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Risk Scorer",
                        "instructions": (
                            """You are a customer-success analyst. Given the account health data (CSV) and the risk signal definitions (e.g., negative usage trend, failure rate above 10%, sponsor untouched for 30+ days):
1. Evaluate every signal per account and compute a composite risk score on a 0-100 scale; show the per-signal contribution so the score is explainable.
2. Use the per-signal weights provided in the inputs. If weights are not provided, use these defaults and state them in the output: usage trend 0.35, failure rate 0.30, sponsor inactivity 0.20, NPS/CSAT 0.15.
3. Use the high-risk threshold provided in the inputs. If not provided, default to composite score >= 70 and state it.
4. Rank accounts by risk; for each, state the dominant driver in one sentence grounded in the data.
Emit a structured result that sets the value the workflow branches on: input.high_risk_found == true (true only if at least one account meets or exceeds the threshold).
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the account health data, the signal definitions)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "risk-threshold-check", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "cases": [
                            {"id": "case-high-risk", "name": "High-risk accounts", "expression": "input.high_risk_found == true"},
                        ],
                    },
                },
                {
                    "id": "outreach-drafter", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Outreach Drafter",
                        "instructions": (
                            """High-risk accounts need attention. For the top at-risk accounts, draft personalized outreach for the account owner that names the specific observed issue (e.g., rising failure rate) and proposes a concrete next step (working session, escalation review) -- empathetic, never alarmist, and only citing data the account would recognize. For each account, include a one-line CSM play summary (risk driver and recommended play) so the CSM can act on it. Produce the final deliverable as a Word document (.docx) - the outreach pack labelled DRAFT - CSM review, with a per-account play summary at the top, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.5, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {
                    "id": "health-reporter", "type": "agent",
                    "position": {"x": 1000, "y": 310},
                    "data": {
                        "name": "Health Reporter",
                        "instructions": (
                            """No accounts cross the risk threshold. Produce the final deliverable as an Excel spreadsheet (.xlsx) - the portfolio health report (account, score, per-signal status, trend) with a watchlist tab naming accounts closest to the threshold to monitor next cycle, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1250, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "risk-scorer", "sourceHandle": "body"},
                {"id": "e3", "source": "risk-scorer", "target": "risk-threshold-check"},
                {"id": "e4", "source": "risk-threshold-check", "target": "outreach-drafter", "sourceHandle": "case-high-risk"},
                {"id": "e5", "source": "risk-threshold-check", "target": "health-reporter", "sourceHandle": "else"},
                {"id": "e6", "source": "outreach-drafter", "target": "loop"},
                {"id": "e7", "source": "health-reporter", "target": "loop"},
                {"id": "e8", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── UC-100 · Personalized Learning Tutor · Viable-32 · instant tier ──
    {
        "id": "template-personalized-learning-tutor",
        "name": "Personalized Learning Tutor",
        "description": "[UC-100 | Viable-32 | instant tier] Assess a learner's level and goals, then build an adaptive learning plan with milestones and checkpoints.",
        "category": "Research & Exec",
        "usecase_uc": 100, "execution_tier": "instant", "viable_32": True,
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "loop", "type": "loop",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "label": "For each learners",
                        "mode": "for_each",
                        "itemsExpression": "input.learners",
                        "iteratorVar": "item",
                        "count": 0,
                        "cases": [],
                        "maxIterations": 50,
                    },
                },
                {
                    "id": "learner-assessor", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Learner Assessor",
                        "instructions": (
                            """You are a learning advisor. Given the learner intake (role, goal, time budget, self-assessment, learning-style preference) and diagnostic results:
1. Read the intake and the content catalog.
2. Identify the gap between current level and the stated goal, prioritizing the diagnostic's weak areas over self-assessment where they disagree.
3. Select catalog modules that close the gaps, respecting the weekly time budget and the learner's preference (e.g., hands-on over video).
4. Tie-break when multiple catalog modules close the same gap within the time budget: pick by (a) match to the learner's stated preference (hands-on > video > reading if hands-on is preferred), then (b) shortest duration, then (c) highest catalog rating. If still tied, present both to the next agent as alternatives rather than picking arbitrarily.
Pass the gap analysis and module selection to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs (the intake, the diagnostic results, the catalog)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "plan-builder", "type": "agent",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "name": "Plan Builder",
                        "instructions": (
                            """You build the learning plan. Using the gap analysis and selected modules:
1. Sequence modules week by week within the time budget, weak areas first, each week ending with a milestone the learner can demonstrate ('build and run a template chain') and a short checkpoint quiz topic.
2. Define the adaptation rule per checkpoint: what to repeat or skip based on the result.
3. Produce two final deliverables - (a) a Word document (.docx) learner-facing plan (encouraging, concrete) and (b) an Excel spreadsheet (.xlsx) progress tracker (week, modules, hours, milestone, checkpoint-result column) - using the attached skill(s); keep them clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or policy terms. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.4, "maxTokens": 4096, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1000, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "learner-assessor", "sourceHandle": "body"},
                {"id": "e3", "source": "learner-assessor", "target": "plan-builder"},
                {"id": "e4", "source": "plan-builder", "target": "loop"},
                {"id": "e5", "source": "loop", "target": "end", "sourceHandle": "exit"},
            ],
        },
    },
    # ── Incident Root Cause Analysis (RCA) · conditional ──
    {
        "id": "template-incident-rca",
        "name": "Incident Root Cause Analysis",
        "description": "Reconstruct an incident timeline, perform structured root cause analysis (5-whys), and produce a blameless postmortem for Sev1/Sev2 incidents or a lightweight RCA note otherwise.",
        "category": "Operations",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "timeline-builder", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Timeline Builder",
                        "instructions": (
                            """You are an incident-response analyst. Given the incident inputs (alerts, monitoring/log excerpts, chat/ops transcript, deploy history):
1. Reconstruct a chronological timeline of the incident: detection, escalation, mitigation attempts, and resolution.
2. For every entry, record the exact timestamp and quote the source line verbatim (log line, alert, or message) -- timelines are only as good as their evidence.
3. Mark the detection point, the customer-impact window (start and end), and the mitigation/resolution point explicitly.
4. If a critical fact is missing (e.g., no resolution timestamp), record an explicit gap entry rather than inferring it.
Pass the reconstructed timeline to the next agent.
Ground every statement in the inputs -- never invent timestamps, service names, error codes, or people. Ask only for missing business inputs (the logs, alerts, or transcript)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.2, "maxTokens": 32000, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "root-cause-analyzer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Root Cause Analyzer",
                        "instructions": (
                            """You perform structured root cause analysis on the reconstructed timeline.
1. Apply the 5-whys technique from the customer-impacting symptom down to the underlying cause; show each 'why' and its evidence from the timeline.
2. Separate the trigger (what set the incident off) from the root cause (the underlying weakness) from contributing factors (things that made it worse or slower to detect).
3. Assess severity from customer impact and duration and classify it: SEV1 (critical, broad customer impact / data loss), SEV2 (major, partial impact), or SEV3+ (minor/internal).
4. Emit a structured result that sets the value the workflow branches on: input.is_sev1_or_sev2 == true when severity is SEV1 or SEV2.
Ground every statement in the timeline and inputs -- never invent facts, figures, names, or codes. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 32000, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "severity-check", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "label": "Sev1 / Sev2?",
                        "cases": [
                            {
                                "id": "case-major",
                                "label": "Sev1 / Sev2",
                                "logic": "AND",
                                "conditions": [
                                    {
                                        "id": "cond-is-sev1-or-sev2",
                                        "field": "is_sev1_or_sev2",
                                        "operator": "==",
                                        "value": True,
                                        "type": "boolean",
                                    }
                                ],
                            },
                        ],
                    },
                },
                {
                    "id": "postmortem-author", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Postmortem Author",
                        "instructions": (
                            """This is a Sev1/Sev2 incident. Produce the final deliverable as a Word document (.docx) - a blameless postmortem containing: summary, impact (who/how many/how long), the reconstructed timeline, the 5-whys root cause, contributing factors, and a corrective-action table (action, owner, due date, priority) with at least one preventive action addressing the root cause. Keep the tone blameless (focus on systems and process, not individuals), using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or codes. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 32000, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {
                    "id": "rca-summary-writer", "type": "agent",
                    "position": {"x": 1000, "y": 310},
                    "data": {
                        "name": "RCA Summary Writer",
                        "instructions": (
                            """This is a lower-severity incident (Sev3+). Produce the final deliverable as a Word document (.docx) - a concise RCA note containing: a one-paragraph summary, the trigger and root cause, the contributing factors, and one or two follow-up actions with owners. Keep it short and practical, using the attached skill(s); keep it clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or codes. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 32000, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1250, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "timeline-builder"},
                {"id": "e2", "source": "timeline-builder", "target": "root-cause-analyzer"},
                {"id": "e3", "source": "root-cause-analyzer", "target": "severity-check"},
                {"id": "e4", "source": "severity-check", "target": "postmortem-author", "sourceHandle": "case-major"},
                {"id": "e5", "source": "severity-check", "target": "rca-summary-writer", "sourceHandle": "else"},
                {"id": "e6", "source": "postmortem-author", "target": "end"},
                {"id": "e7", "source": "rca-summary-writer", "target": "end"},
            ],
        },
    },
    # ── Enterprise Risk Assessment · conditional + human approval (HITL) ──
    {
        "id": "template-risk-assessment",
        "name": "Enterprise Risk Assessment",
        "description": "Identify risks across an initiative, score them on a likelihood x impact matrix, route high residual risks through a human owner review (HITL), and produce a risk register and executive summary.",
        "category": "Compliance",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 200}, "data": {"label": "Start"}},
                {
                    "id": "risk-identifier", "type": "agent",
                    "position": {"x": 250, "y": 200},
                    "data": {
                        "name": "Risk Identifier",
                        "instructions": (
                            """You are a risk analyst. Given the initiative/scope description and any supporting context (project brief, controls inventory, prior incidents):
1. Read the scope and context.
2. Enumerate distinct risks across categories: operational, compliance/regulatory, security, financial, and reputational. Cover each category or explicitly state why it does not apply.
3. For each risk, write a clear risk statement in cause -> event -> consequence form, and note any existing control mentioned in the inputs.
4. If a category cannot be assessed for lack of information, record an explicit gap entry rather than omitting it silently.
Pass the risk inventory to the next agent.
Ground every statement in the inputs -- never invent risks, figures, systems, or controls that are not supported by the inputs. Ask only for missing business inputs (the scope/brief, the controls inventory)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 32000, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "risk-scorer", "type": "agent",
                    "position": {"x": 500, "y": 200},
                    "data": {
                        "name": "Risk Scorer",
                        "instructions": (
                            """You score each identified risk.
1. Rate Likelihood (1-5) and Impact (1-5) for each risk, with a one-line justification grounded in the inputs.
2. Compute inherent risk = Likelihood x Impact and map it to a band (Low 1-6, Medium 8-12, High 15-25).
3. Factor in existing controls to estimate residual risk and its band.
4. Rank risks by residual risk, highest first.
5. Emit a structured result that sets the value the workflow branches on: input.has_high_residual_risk == true when any risk has a High residual band.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or controls. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 32000, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "risk-gate", "type": "condition",
                    "position": {"x": 750, "y": 200},
                    "data": {
                        "label": "High residual risk?",
                        "cases": [
                            {
                                "id": "case-high",
                                "label": "High residual risk",
                                "logic": "AND",
                                "conditions": [
                                    {
                                        "id": "cond-has-high-residual-risk",
                                        "field": "has_high_residual_risk",
                                        "operator": "==",
                                        "value": True,
                                        "type": "boolean",
                                    }
                                ],
                            },
                        ],
                    },
                },
                {
                    "id": "risk-owner-review", "type": "agent",
                    "position": {"x": 1000, "y": 90},
                    "data": {
                        "name": "Risk Owner Review",
                        "instructions": (
                            """High residual risks are present, so a human risk owner must review before the register is finalized. Prepare a concise decision brief for the reviewer: the high residual risks ranked by score, the proposed treatment for each (mitigate / transfer / accept / avoid) with a recommended owner and target date, and any risk you recommend the owner formally accept. Present this clearly and then pause for the human's decision.
After the human responds, incorporate their decisions (accepted risks, revised owners/dates, added treatments) and pass the reconciled, owner-reviewed risk set to the next agent.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or controls. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 32000, "topP": 1.0, "baseUrl": "",
                        "hitlMode": "off",
                        "tools": [],
                    },
                },
                {
                    "id": "risk-register-builder", "type": "agent",
                    "position": {"x": 1250, "y": 200},
                    "data": {
                        "name": "Risk Register Builder",
                        "instructions": (
                            """Produce the final deliverables from the scored (and, where applicable, owner-reviewed) risks:
1. An Excel spreadsheet (.xlsx) risk register with one row per risk: ID, category, risk statement, likelihood, impact, inherent score/band, existing controls, residual score/band, treatment (mitigate/transfer/accept/avoid), owner, target date, and status.
2. A Word document (.docx) executive summary: the overall risk posture, the top residual risks called out on top, any risks formally accepted by the owner, and the recommended next actions.
Use the attached skill(s); keep both deliverables clean, consistently formatted, and ready to share.
Ground every statement in the inputs and the previous stage's output -- never invent facts, figures, names, or controls. Ask only for missing business inputs."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": "",
                        "temperature": 0.3, "maxTokens": 32000, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1500, "y": 200}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "risk-identifier"},
                {"id": "e2", "source": "risk-identifier", "target": "risk-scorer"},
                {"id": "e3", "source": "risk-scorer", "target": "risk-gate"},
                {"id": "e4", "source": "risk-gate", "target": "risk-owner-review", "sourceHandle": "case-high"},
                {"id": "e5", "source": "risk-gate", "target": "risk-register-builder", "sourceHandle": "else"},
                {"id": "e6", "source": "risk-owner-review", "target": "risk-register-builder"},
                {"id": "e7", "source": "risk-register-builder", "target": "end"},
            ],
        },
    },
    # ── BRD Generation Workflow ──────────────────────────────────────────
    {
        "id": "template-brd-generation",
        "name": "BRD Generation",
        "description": "Generate a board-ready Business Requirements Document with risk register and committee pack from a raw product idea or business need.",
        "category": "Research & Exec",
        "graph_data": {
            "nodes": [
                {"id": "start", "type": "start", "position": {"x": 50, "y": 300}, "data": {"label": "Start"}},
                {
                    "id": "requirements-analyst", "type": "agent",
                    "position": {"x": 300, "y": 300},
                    "data": {
                        "name": "Requirements Analyst",
                        "instructions": (
                            """You are a senior business analyst specializing in product requirements.

Do NOT delegate to sub-agents.
Do NOT generate files.
Output structured text only.

Given a raw product idea, feature request, or business need:

1. Extract the core business goals.

2. Define 3-5 measurable success metrics (KPIs).
   - Use target values only if explicitly provided.
   - If target values are missing, mark them as:
     "TBD – stakeholder input required."

3. Identify user personas explicitly mentioned or strongly implied in the input.
   For each persona, provide:
   - Role
   - Pain point
   - Definition of success

   If personas cannot be determined from the input, state so explicitly.

4. Define scope clearly:
   - In Scope: features, integrations, deliverables
   - Out of Scope: items explicitly excluded or not supported by the provided information

   Mark any inferred scope items as assumptions.

5. Write a high-level solution overview (3-4 sentences) describing:
   - What the product does
   - How it works conceptually
   - Key differentiators

6. Identify any stated or implied non-functional requirements
   (performance, security, compliance, availability, scalability).
   If none exist, state "Not specified."

7. List assumptions and open questions requiring stakeholder clarification.

Output a structured requirements document with the following sections:

- Business Goals & KPIs
- User Personas
- Scope (In Scope / Out of Scope)
- Solution Overview
- Non-Functional Requirements
- Assumptions
- Open Questions

Ground every statement in the user's input.
Do not invent business goals, KPIs, target values, user roles, requirements, integrations, or scope items.
If information is missing, insufficient, or ambiguous, explicitly state that stakeholder input is required.

Produce the output in a format suitable for direct handoff to other agents."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.1, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "risk-analyzer", "type": "agent",
                    "position": {"x": 650, "y": 80},
                    "data": {
                        "name": "Risk Analyzer",
                        "instructions": (
                            """You are a Risk and Compliance Analyst responsible for evaluating delivery, business, operational, technical, and regulatory risks.

Do ALL work directly yourself.
Do NOT delegate to sub-agents.
Do NOT generate files.
Output structured text only.

Using the structured requirements provided by the previous agent:

1. Identify Risks
   - Identify up to 10 risks that are directly supported by the requirements.
   - Consider risks across:
     - Technical
     - Operational
     - Regulatory / Compliance
     - Resource
     - Market / Business
   - If fewer than 5 grounded risks can be identified, report only those supported by the requirements and note the limitation.

2. Assess Each Risk

Risk Scoring Methodology:
- High = 3
- Medium = 2
- Low = 1

Risk Score = Likelihood × Impact

Risk Levels:
- 1–2 = Low
- 3–4 = Medium
- 6–9 = High

For each risk provide:
- Risk ID
- Category
- Description
- Likelihood
- Impact
- Risk Score
- Mitigation Strategy
- Owner
- Status (default: Open)

Mitigations must be specific and actionable.
Avoid generic recommendations.

3. Compliance and Regulatory Review
   - Identify any requirements related to:
     - Data privacy
     - Security
     - Governance
     - Auditability
     - Industry regulations
     - Cross-border data handling
   - Only include items explicitly stated or reasonably implied by the requirements.

   - If none are identified, state:
     "No regulatory or compliance implications identified from the current requirements."

4. Dependency Analysis
   - Identify dependencies that could affect delivery, including:
     - Internal teams
     - External vendors
     - Third-party systems
     - Integrations
     - Infrastructure

5. Overall Risk Assessment
   - Count the number of High-Risk items (Risk Score ≥ 6).

If High-Risk Count ≥ 3:

RISK LEVEL: HIGH — 3+ critical risks identified.
Recommend additional review before proceeding.

Otherwise:

RISK LEVEL: ACCEPTABLE — risks are manageable with stated mitigations.

6. Assumptions
   - Document any assumptions made during risk identification.

Output Format

# Risk Register

| Risk ID | Category | Description | Likelihood | Impact | Risk Score | Mitigation Strategy | Owner | Status |
|----------|----------|-------------|------------|--------|------------|--------------------|--------|--------|

(Sorted by Risk Score descending)

# Compliance & Regulatory Considerations
...

# Key Dependencies
...

# Assumptions
...

# Overall Risk Assessment
...

Rules

- Ground every risk in the supplied requirements.
- Do not invent risks, dependencies, compliance obligations, systems, vendors, stakeholders, or mitigation activities that are unsupported by the requirements.
- Clearly indicate when information is missing or insufficient.
- Prefer "Not Specified" over making assumptions.
- Sort the Risk Register by Risk Score (highest first)."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.1, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "committee-pack-builder", "type": "agent",
                    "position": {"x": 650, "y": 300},
                    "data": {
                        "name": "Committee Pack Builder",
                        "instructions": (
                            """You are an Executive Communications Specialist responsible for creating board-ready and committee-ready review pack outlines.

Do ALL work directly yourself.
Do NOT delegate to sub-agents.
Do NOT generate files.
Output structured text only.

Using the structured requirements provided by the Requirements Analyst, create a detailed Committee Review Pack outline.

For each section, draft the exact headings, bullet points, and table structures that should appear in the final committee pack.

If required information is missing, use:
[TBD – Stakeholder Input Required]

Sections

1. Executive Brief
   - Proposal summary
   - Business rationale
   - Strategic alignment
   - Decision required

2. Key Stakeholders
   - Stakeholder
   - Role
   - Impact
   - Approval responsibility

3. Business Case Summary
   - Current problem
   - Proposed solution
   - Expected business value
   - Success criteria
   - ROI (if available; otherwise mark TBD)

4. Implementation Roadmap
   - Phase
   - Objective
   - Key milestones
   - Timeline (if available; otherwise mark TBD)
   - Resource requirements

5. Budget & Resource Ask
   - Requested item
   - Type (Budget / Headcount / Infrastructure / Vendor)
   - Business justification
   - Amount or quantity (if available; otherwise mark TBD)

6. Compliance & Regulatory Considerations
   - Regulatory requirements
   - Data governance considerations
   - Security requirements
   - Audit requirements
   - Required approvals

7. Risks & Dependencies
   - Risk or dependency
   - Impact
   - Mitigation approach
   - Owner (if known)

8. Decision Required
   - Approval being requested
   - Decision date or deadline
   - Consequences of delay
   - Next steps after approval

Rules

- Ground every section in the supplied requirements.
- Do not invent facts, figures, timelines, budgets, ROI estimates, resource counts, stakeholders, approvals, risks, or commitments.
- Use only information explicitly provided or clearly supported by the requirements.
- Where information is unavailable, mark it as:
  [TBD – Stakeholder Input Required]
- Keep the structure concise, executive-ready, and suitable for committee review."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.1, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "brd-drafter", "type": "agent",
                    "position": {"x": 650, "y": 520},
                    "data": {
                        "name": "BRD Drafter",
                        "instructions": (
                            """You are a Senior Product Manager specializing in board-ready Business Requirements Documents (BRDs).

Do ALL work directly yourself.
Do NOT delegate to sub-agents.
Do NOT generate files.
Output the BRD as structured text only.

Using the structured requirements provided by the Requirements Analyst:

1. Executive Summary
   - Write a concise executive summary (1–2 paragraphs).
   - Describe:
     - Business problem
     - Proposed solution
     - Expected business value
     - Decision or outcome sought

2. Business Goals & Success Metrics
   Create a table:

   | Goal | KPI | Target | Measurement Method |

   - Use only goals and metrics contained in the requirements.
   - If targets or measurement methods are unavailable, use:
     [TBD – Stakeholder Input Required]

3. User Personas
   For each identified persona provide:
   - Role
   - Responsibilities
   - Pain Points
   - Success Criteria
   - User Journey Narrative

   Include a brief "Day in the Life" scenario based only on information available in the requirements.

   If sufficient information is unavailable, note:
   [TBD – Additional User Research Required]

4. Scope Definition
   Create a boundary table:

   | Item | In Scope / Out of Scope | Rationale |

   Only include scope items identified in the requirements.

5. Solution Overview
   Describe:
   - Business context
   - Conceptual solution approach
   - Key components (if specified)
   - Integration points (if specified)

   If components or integrations are not identified, indicate:
   "Not specified in current requirements."

6. Functional Requirements
   Create uniquely numbered requirements:

   - FR-001
   - FR-002
   - etc.

   Each requirement must trace directly back to an approved scope item.

7. Dependencies & Constraints

   Dependencies:
   - Teams
   - Systems
   - Vendors
   - External services

   Constraints:
   - Budget
   - Timeline
   - Compliance
   - Technology limitations

   Indicate "Not specified" where appropriate.

8. Assumptions & Open Questions
   - Assumptions inherited from the requirements analysis
   - Outstanding questions requiring stakeholder input

Output Format

# Executive Summary

# Business Goals & Success Metrics

# User Personas

# Scope Definition

# Solution Overview

# Functional Requirements

# Dependencies & Constraints

# Assumptions & Open Questions

Rules

- Ground every statement in the supplied requirements.
- Do not invent requirements, KPIs, targets, integrations, personas, workflows, dependencies, timelines, budgets, or technical details.
- Clearly distinguish facts from assumptions.
- Mark missing information as:
  [TBD – Stakeholder Input Required]
- Ensure the document is suitable for executive review and direct handoff to delivery teams."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_HAIKU"),
                        "temperature": 0.1, "maxTokens": 8192, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                    },
                },
                {
                    "id": "delivery-coordinator", "type": "agent",
                    "position": {"x": 1000, "y": 300},
                    "data": {
                        "name": "Delivery Coordinator",
                        "instructions": (
                            """You are a Delivery Coordinator responsible for compiling the final BRD Approval Pack into a single professional document.

Do ALL work directly yourself.
Do NOT delegate to sub-agents.
Use the docx skill to generate the final .docx document.

Your role is document assembly only.
Do NOT summarize, rewrite, interpret, or enhance content from upstream agents.

Three agents have already produced structured outputs.

PART 1 — BUSINESS REQUIREMENTS DOCUMENT

Insert the BRD Drafter output exactly as provided:

- Executive Summary
- Business Goals & Success Metrics
- User Personas
- Scope Definition
- Solution Overview
- Functional Requirements (if provided)
- Dependencies & Constraints
- Assumptions & Open Questions

PART 2 — RISK REGISTER

Insert the Risk Assessor output exactly as provided.

Requirements:
- Preserve risk ordering
- Format the Risk Register as a table with columns:
  - Risk ID
  - Category
  - Description
  - Likelihood
  - Impact
  - Risk Score
  - Mitigation Strategy
  - Owner
  - Status
- Display the overall RISK LEVEL verdict prominently before the table.
- Include all compliance considerations and dependencies sections.

PART 3 — COMMITTEE PRESENTATION OUTLINE (APPENDIX)

Insert the Committee Pack Builder output exactly as provided.

Include:
- Executive Brief
- Key Stakeholders
- Business Case Summary
- Implementation Roadmap
- Budget & Resource Ask
- Compliance & Regulatory Considerations
- Risks & Dependencies (if provided)
- Decision Required

Formatting Requirements

Cover Page:
- [Product Name] — BRD & Approval Pack
- Date
- Prepared for PAC/SAC/IRMC Review
- Document Version
- Prepared By

Document Structure:
- Table of Contents
- Consistent heading hierarchy
- Professional formatting
- Page break between each major part

Handling Missing Information

If content is missing from an upstream agent, insert:

[TBD – Not Provided by Upstream Agent]

Do not generate replacement content.

Rules

- Preserve upstream content exactly.
- Do not add, remove, infer, embellish, summarize, or modify content.
- Your responsibility is compilation and formatting only.
- Ensure all tables remain tables.
- Produce a final professional .docx document suitable for executive review and approval workflows."""
                        ),
                        "provider": "custom", "apiKey": "", "modelName": _tmpl_model("CLAUDE_OPUS_MODEL"),
                        "temperature": 0.1, "maxTokens": 32000, "topP": 1.0, "baseUrl": "",
                        "tools": [],
                        "skills": [
                            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
                        ],
                    },
                },
                {"id": "end", "type": "end", "position": {"x": 1300, "y": 300}, "data": {"label": "End"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "requirements-analyst"},
                {"id": "e2", "source": "requirements-analyst", "target": "risk-analyzer"},
                {"id": "e3", "source": "requirements-analyst", "target": "committee-pack-builder"},
                {"id": "e4", "source": "requirements-analyst", "target": "brd-drafter"},
                {"id": "e5", "source": "risk-analyzer", "target": "delivery-coordinator"},
                {"id": "e6", "source": "committee-pack-builder", "target": "delivery-coordinator"},
                {"id": "e7", "source": "brd-drafter", "target": "delivery-coordinator"},
                {"id": "e8", "source": "delivery-coordinator", "target": "end"},
            ],
        },
    },
])



# ===========================================================================
# In-repo MCP servers under backend/app/tools/ were removed from the product
# toolset. Keep this post-pass empty so startup refreshes templates without
# reattaching deleted MCP server/tool names.
# ===========================================================================
_UC_MCP_AGENT_MAP: Dict[int, Dict[str, List[str]]] = {}


_MCP_SERVER_TOOLSETS: Dict[str, List[tuple]] = {}


def _apply_mcp_nodes_to_template(template: dict, agent_to_mcp: Dict[str, List[str]]) -> None:
    """Mutate `template` in place: for each agent→server pairing, append the
    server's canonical tool functions onto the agent's ``data.tools`` array.

    This used to spawn separate ``type: "mcp"`` nodes on the graph and wire
    them with MCP→agent edges; the resulting "Data Tools" rectangles
    floated below the canvas and confused the layout. The runtime engine
    already resolves any ``<service>__<fn>`` name through the catalog, so a
    plain tool attachment is exactly equivalent — and renders inline as a
    tool chip on the agent card.

    Idempotent: re-applying skips tools that are already on the agent and
    leaves an MCP-free graph if the function is re-run.
    """
    graph = template.get("graph_data") or {}
    nodes = graph.setdefault("nodes", [])
    edges = graph.setdefault("edges", [])

    # ── 1. Purge any legacy MCP nodes + edges left over from older seeds.
    #     The graph stays linear / agent-only after this pass.
    mcp_node_ids = {n["id"] for n in nodes if n.get("type") == "mcp"}
    if mcp_node_ids:
        graph["nodes"] = [n for n in nodes if n.get("id") not in mcp_node_ids]
        nodes = graph["nodes"]
        graph["edges"] = [
            e for e in edges
            if e.get("source") not in mcp_node_ids
            and e.get("target") not in mcp_node_ids
        ]
        edges = graph["edges"]

    # ── 2. For each (agent, [server, ...]) pairing, append the server's
    #     canonical tool functions to the agent's tools array.
    all_servers: List[str] = []
    for agent_id, servers in agent_to_mcp.items():
        agent_node = next(
            (n for n in nodes if n.get("id") == agent_id and n.get("type") == "agent"),
            None,
        )
        if agent_node is None:
            # Defensive: the seed structure changed; skip rather than crash
            # the boot.
            continue
        data = agent_node.setdefault("data", {})
        tools = data.setdefault("tools", [])
        # Index by name so re-applies stay idempotent.
        existing_names = {(t.get("name") or "").strip() for t in tools}

        for server_type in servers:
            all_servers.append(server_type)
            for fn_name, fn_desc in _MCP_SERVER_TOOLSETS.get(server_type, []):
                if fn_name in existing_names:
                    continue
                tools.append({"name": fn_name, "description": fn_desc})
                existing_names.add(fn_name)

    # ── 3. Strip any legacy `[MCP: ...]` marker from the description.
    #     Earlier revisions stamped the marker into the description so the
    #     catalog UI could surface server dependencies; with MCP tools now
    #     inlined as tool chips on the agent card, the marker is redundant
    #     and was leaking into the templates page as the leading text on
    #     each card (the FE hides the `[UC-...]` tag but rendered the
    #     `[MCP:...]` tag). Templates expose the same information via
    #     `agent.data.tools` — the marker just adds noise.
    import re as _re
    desc = template.get("description") or ""
    if "[MCP:" in desc:
        cleaned = _re.sub(r"\s*\[MCP:[^\]]*\]\s*", " ", desc).strip()
        # Collapse double spaces and trim any leftover whitespace.
        cleaned = _re.sub(r"\s{2,}", " ", cleaned)
        template["description"] = cleaned


# Index templates by their declared usecase_uc once, then apply the mapping.
_templates_by_uc = {
    t["usecase_uc"]: t
    for t in _SEED_TEMPLATES
    if isinstance(t.get("usecase_uc"), int)
}
for _uc, _agent_map in _UC_MCP_AGENT_MAP.items():
    _tpl = _templates_by_uc.get(_uc)
    if _tpl is not None:
        _apply_mcp_nodes_to_template(_tpl, _agent_map)


# ===========================================================================
# Pattern reclassification + structural rebuild
# ===========================================================================
# The seed templates were originally authored as flat sequential chains (or
# sequential→single-condition graphs). Many real use cases — per-record
# batch processing, multi-source gathering, write-action approvals — are not
# actually sequential, so the catalog UI's filter chips lied. This section:
#
#   1. Stamps `pattern` (sequential / parallel / conditional / loop /
#      parallel_conditional / loop_conditional) on every hand-written
#      template via `_PATTERN_OVERRIDES`.
#   2. Stamps additional `hitlMode` values on agents that perform
#      irreversible writes via `_PATTERN_HITL_OVERRIDES` (additive to the
#      pre-existing `_HITL_GATES` set).
#   3. Restructures `graph_data` for templates whose real shape is parallel
#      / loop / loop+condition. The rebuild WRAPS the existing agent nodes
#      (preserving every instruction, tool, skill, and MCP attachment)
#      rather than rewriting them — only the edges, the synthetic
#      condition/loop nodes, and the node positions change.
#
# Auto-generated templates (`_NON_ENGINEERING_USECASES`) already derive
# `pattern` from their `stages` shape inside `_build_usecase_workflow`
# (see line ~1574) and are skipped by this pass.
# ===========================================================================

# ─── Pattern vocabulary ─────────────────────────────────────────────────
#   sequential            — linear chain.
#   parallel              — one fan-out → join.
#   conditional           — chain ending in a condition with ≥2 branches.
#   loop                  — contains a loop node (for_each by default).
#   parallel_conditional  — parallel fan-out and a condition branch.
#   loop_conditional      — loop body contains a condition (per-item routing).
_VALID_PATTERNS = frozenset({
    "sequential", "parallel", "conditional",
    "loop", "parallel_conditional", "loop_conditional",
})

# Per-template pattern. Anything not listed defaults to `sequential` after
# the post-pass runs (see `_apply_post_pass_to_template`). Auto-generated
# UCs are NOT keyed here — `_build_usecase_workflow` already sets their
# pattern from `stages`.
_PATTERN_OVERRIDES: Dict[str, str] = {
    # ── Engineering (7) ────────────────────────────────────────────────
    "template-sdlc-feature-flow":              "sequential",
    "template-sdlc-bug-flow":                  "sequential",
    "template-code-review":                    "sequential",
    "template-appsec-mr-scan":                 "conditional",
    "template-jira-issue-triage":              "sequential",
    "template-gitlab-mr-to-jira":              "sequential",
    "template-release-notes-generator":        "sequential",

    # ── HR & Recruiting (9) ────────────────────────────────────────────
    "template-policy-document-generator":      "sequential",
    "template-training-plan-creator":          "sequential",
    "template-jd-writer":                      "sequential",
    "template-resume-screening-report":        "sequential",
    "template-resume-to-jd-matching":          "conditional",
    "template-interview-scheduling":           "sequential",
    "template-candidate-follow-up-sequences":  "sequential",
    "template-employee-onboarding-coordination":"parallel",
    "template-hr-policy-qa":                   "sequential",

    # ── Finance (5) ────────────────────────────────────────────────────
    "template-invoice-processing":             "conditional",
    "template-expense-categorization":         "conditional",
    "template-financial-reconciliation":       "conditional",
    "template-budget-variance-analysis":       "sequential",
    "template-financial-report-generation":    "sequential",

    # ── Legal & Compliance (4) ─────────────────────────────────────────
    # DSLAR is a real parallel+conditional graph: a validation-type router
    # (report vs DL-SAR) plus a 4-way clause-validator fan-out that joins on
    # the results aggregator. The graph already encodes this correctly, so
    # this override only fixes the catalog label (there is no _PARALLEL_SPECS
    # entry, so _rewire_as_parallel is a no-op and the graph is untouched).
    "template-dslar-ainxt-audit-validation":    "parallel_conditional",
    "template-contract-review":                "conditional",
    "template-legal-document-summarization":   "sequential",
    "template-compliance-checklist-auditing":  "conditional",

    # ── Risk & Incident (2) ────────────────────────────────────────────
    "template-incident-rca":                   "conditional",
    "template-risk-assessment":                "conditional",

    # ── Support & Operations (7) ───────────────────────────────────────
    "template-support-ticket-triage":          "conditional",
    "template-kb-grounded-response-drafting":  "sequential",
    "template-end-to-end-ticket-resolution":   "conditional",
    "template-executive-inbox-triage":         "sequential",
    "template-calendar-management":            "sequential",
    "template-meeting-notes-summarizer":       "sequential",
    "template-meeting-notes-action-items":     "sequential",

    # ── Sales & Marketing (6) ──────────────────────────────────────────
    "template-content-repurposing":            "sequential",
    "template-social-media-scheduling":        "sequential",
    "template-press-release-drafting":         "sequential",
    "template-customer-feedback-theme-extraction": "sequential",
    "template-rfp-response-drafting":          "sequential",
    "template-churn-risk-scoring-outreach":    "conditional",

    # ── Analytics & Reporting (3) ──────────────────────────────────────
    "template-survey-design-analysis":         "sequential",
    "template-data-analysis-charting":         "sequential",
    "template-dashboard-deck-generation":      "sequential",

    # ── Knowledge & Learning (4) ───────────────────────────────────────
    "template-document-translation-localization": "sequential",
    "template-policy-sop-drafting":            "sequential",
    "template-training-material-creation":     "sequential",
    "template-personalized-learning-tutor":    "sequential",
}


# Extra HITL gates layered on top of the existing `_HITL_GATES`. These mark
# agents that perform irreversible writes (Jira transitions, outbound
# emails, social-media scheduling, financial postings) so the runtime
# pauses for human approval before the tool fires. Modes:
#   "before_tool"    — pause before a destructive tool call
#   "after_response" — pause once the agent has produced its draft
#   "both"           — pause at both points
#
# NOTE: Human-in-the-loop gates have been removed from all templates. This
# table is intentionally empty so the rebuild pass layers no HITL modes.
# Users can re-enable HITL per agent manually in the canvas editor.
_PATTERN_HITL_OVERRIDES: Dict[tuple, str] = {}

# Indexed-by-template view of the same overrides. Built once at import so
# the rebuild pass only walks node lists for templates that actually have
# a HITL override to apply (skips the 51 templates that don't).
_PATTERN_HITL_BY_TPL: Dict[str, Dict[str, str]] = {}
for (_tpl_key, _node_key), _mode in _PATTERN_HITL_OVERRIDES.items():
    _PATTERN_HITL_BY_TPL.setdefault(_tpl_key, {})[_node_key] = _mode


# Helper builders. They mirror the in-line dict shapes used by the seed
# literals above so the post-pass (`_normalise_*`) finds every field where
# it expects to. Keep the field ordering identical to make round-tripping
# through JSON serialisation deterministic.

# Canvas layout grid used by the structural rebuilders below. Centralising
# these means a future canvas-zoom or readability tweak is a one-line edit.
_LAYOUT_COL_W   = 280     # horizontal step between consecutive nodes
_LAYOUT_ROW_Y   = 200     # baseline y for the main row
_LAYOUT_BRANCH_Y_TOP    = 80    # y for the YES branch in conditional rows
_LAYOUT_BRANCH_Y_BOTTOM = 320   # y for the ELSE / NO branch


def _mk_start(x: int = 50, y: int = _LAYOUT_ROW_Y) -> dict:
    return {"id": "start", "type": "start",
            "position": {"x": x, "y": y}, "data": {"label": "Start"}}


def _mk_end(x: int, y: int = _LAYOUT_ROW_Y, node_id: str = "end") -> dict:
    return {"id": node_id, "type": "end",
            "position": {"x": x, "y": y}, "data": {"label": "End"}}


def _mk_loop(node_id: str, *, x: int, y: int = _LAYOUT_ROW_Y,
             label: str = "Loop",
             items_expression: str = "input.items",
             iterator_var: str = "item",
             mode: str = "for_each",
             max_iterations: int = 50,
             count: int = 0) -> dict:
    """Build a loop node. The engine reads config off `node.data` OR the
    top level (`workflowStore.js` flattens on persist) — we set under
    `data` so the canvas editor finds the fields."""
    return {
        "id": node_id, "type": "loop",
        "position": {"x": x, "y": y},
        "data": {
            "label":           label,
            "mode":            mode,
            "itemsExpression": items_expression,
            "iteratorVar":     iterator_var,
            "count":           count,
            "cases":           [],
            "maxIterations":   max_iterations,
        },
    }


def _mk_edge(source: str, target: str, *,
             source_handle: Optional[str] = None) -> dict:
    handle_suffix = f"_{source_handle}" if source_handle else ""
    e: dict = {
        "id":     f"e_{source}_{target}{handle_suffix}",
        "source": source,
        "target": target,
    }
    if source_handle:
        e["sourceHandle"] = source_handle
    return e


# ── Structural rebuild specs ───────────────────────────────────────────
# The rebuilder never invents agents — it walks the existing `nodes` list,
# picks the agents named below, and rewires edges around them via fresh
# loop / condition / start / end nodes.

# Per-loop-template list source. Spelling it out per-template surfaces a
# meaningful hint in the editor's connection-aware list picker instead of
# the generic `input.items` fallback.
_LOOP_ITEMS_EXPR: Dict[str, str] = {
    "template-jira-issue-triage":              "input.issues",
    "template-gitlab-mr-to-jira":              "input.mrs",
    "template-resume-screening-report":        "input.resumes",
    "template-resume-to-jd-matching":          "input.resumes",
    "template-candidate-follow-up-sequences":  "input.candidates",
    "template-invoice-processing":             "input.invoices",
    "template-expense-categorization":         "input.expenses",
    "template-financial-reconciliation":       "input.transactions",
    "template-legal-document-summarization":   "input.documents",
    "template-compliance-checklist-auditing":  "input.controls",
    "template-content-repurposing":            "input.channels",
    "template-social-media-scheduling":        "input.posts",
    "template-support-ticket-triage":          "input.tickets",
    "template-end-to-end-ticket-resolution":   "input.tickets",
    "template-executive-inbox-triage":         "input.emails",
    "template-churn-risk-scoring-outreach":    "input.accounts",
    "template-document-translation-localization": "input.documents",
    "template-personalized-learning-tutor":    "input.learners",
}


# Body-chain agent IDs in execution order. For loop_conditional shapes
# the chain contains a `condition` node mid-chain; everything after it
# belongs to the YES/ELSE branches (the rebuilder splits the tail in half
# unless there's only one branch agent).
_LOOP_BODY: Dict[str, List[str]] = {
    "template-jira-issue-triage":              ["triager"],
    "template-gitlab-mr-to-jira":              ["mr-linker"],
    "template-resume-screening-report":        ["resume-screener"],
    "template-legal-document-summarization":   ["legal-summarizer"],
    "template-content-repurposing":            ["source-analyzer", "channel-adapter"],
    "template-social-media-scheduling":        ["calendar-planner", "copy-drafter"],
    "template-candidate-follow-up-sequences":  ["pipeline-segmenter", "sequence-drafter"],
    "template-document-translation-localization": ["translator", "localization-qa"],
    "template-personalized-learning-tutor":    ["learner-assessor", "plan-builder"],
    # loop_conditional templates — the body chain ends at the condition
    # node; the rebuilder treats the condition's branches as in-body
    # terminators that both return control to the loop's exit edge.
    "template-resume-to-jd-matching":          ["match-scorer", "bar-check",
                                                "shortlister", "decline-drafter"],
    "template-support-ticket-triage":          ["ticket-classifier", "urgency-check",
                                                "escalation-router", "standard-router"],
    "template-end-to-end-ticket-resolution":   ["solution-finder", "resolvability-check",
                                                "resolver", "escalator"],
    "template-invoice-processing":             ["invoice-extractor", "po-validator",
                                                "exception-check",
                                                "exception-flagger", "posting-preparer"],
    "template-expense-categorization":         ["expense-categorizer", "violation-check",
                                                "violation-flagger", "approval-recommender"],
    "template-financial-reconciliation":       ["transaction-matcher", "discrepancy-check",
                                                "discrepancy-investigator", "clean-closer"],
    "template-compliance-checklist-auditing":  ["evidence-mapper", "gap-check",
                                                "gap-reporter", "clean-attestor"],
    "template-churn-risk-scoring-outreach":    ["risk-scorer", "risk-threshold-check",
                                                "outreach-drafter", "health-reporter"],
    "template-executive-inbox-triage":         ["inbox-classifier", "reply-drafter"],
}


# Parallel templates: list the fan-out branch agents that should run in
# parallel from start, plus the downstream "join" agent that consumes
# their combined output. The rebuilder rewires start→[branches]→join→end.
# Only multi-branch specs belong here — single-branch use cases are
# tagged `sequential` instead so the catalog label matches the runtime
# behaviour.
_PARALLEL_SPECS: Dict[str, dict] = {
    "template-employee-onboarding-coordination": {
        "branches": ["checklist-builder", "task-orchestrator"],
        "join":     "readiness-reporter",
    },
}


def _walk_chain(
    by_id: Dict[str, dict],
    ids: List[str],
    *,
    start_x: int,
    y: int,
    prev: str,
    prev_handle: Optional[str],
    new_nodes: List[dict],
    new_edges: List[dict],
) -> tuple:
    """Walk a linear chain of node IDs left-to-right, placing each node at
    `(x, y)`, appending it to `new_nodes`, and connecting it to `prev`
    with `prev_handle` on the first edge only. Returns
    `(next_x, last_id_visited)`. Unknown IDs are skipped silently."""
    x = start_x
    last = prev
    handle: Optional[str] = prev_handle
    for aid in ids:
        node = by_id.get(aid)
        if node is None:
            continue
        node["position"] = {"x": x, "y": y}
        new_nodes.append(node)
        new_edges.append(_mk_edge(last, aid, source_handle=handle))
        last = aid
        handle = None
        x += _LAYOUT_COL_W
    return x, last


def _rewire_as_loop(template: dict) -> None:
    """Wrap the body-chain agents inside a for_each loop. Re-uses the
    agent node dicts in place; only edges, the surrounding start/loop/end
    nodes, and per-node positions change."""
    tpl_id = template["id"]
    body_ids = _LOOP_BODY.get(tpl_id)
    if not body_ids:
        return

    graph = template["graph_data"]
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    missing = [aid for aid in body_ids if aid not in by_id]
    if missing:
        logger.warning(f'[AGENT] Loop rebuild for {tpl_id} skipped — missing agent ids: {missing}')
        return

    items_expr = _LOOP_ITEMS_EXPR.get(tpl_id, "input.items")
    loop_id = "loop"
    items_tail = items_expr.split(".")[-1]

    new_nodes: List[dict] = [
        _mk_start(),
        _mk_loop(loop_id, x=250,
                 items_expression=items_expr,
                 label=f"For each {items_tail}"),
    ]
    new_edges: List[dict] = [_mk_edge("start", loop_id)]

    is_cond_loop = (_PATTERN_OVERRIDES.get(tpl_id) == "loop_conditional")
    chain_start_x = 500

    if not is_cond_loop:
        end_x, last = _walk_chain(
            by_id, list(body_ids),
            start_x=chain_start_x, y=_LAYOUT_ROW_Y,
            prev=loop_id, prev_handle="body",
            new_nodes=new_nodes, new_edges=new_edges,
        )
        new_nodes.append(_mk_end(end_x))
        new_edges.append(_mk_edge(last, loop_id))
        new_edges.append(_mk_edge(loop_id, "end", source_handle="exit"))
        graph["nodes"] = new_nodes
        graph["edges"] = new_edges
        return

    # loop_conditional shape:
    #   pre_agents... → condition → [yes_branch..., no_branch...]
    cond_id: Optional[str] = None
    pre_chain: List[str] = []
    branch_agent_ids: List[str] = []
    for aid in body_ids:
        n = by_id.get(aid)
        if n is None:
            continue
        if n.get("type") == "condition":
            cond_id = aid
        elif cond_id is None:
            pre_chain.append(aid)
        else:
            branch_agent_ids.append(aid)

    if cond_id is None or not branch_agent_ids:
        logger.warning(f'[AGENT] loop_conditional rebuild for {tpl_id} skipped — no condition or branches found')
        return

    pre_end_x, pre_last = _walk_chain(
        by_id, pre_chain,
        start_x=chain_start_x, y=_LAYOUT_ROW_Y,
        prev=loop_id, prev_handle="body",
        new_nodes=new_nodes, new_edges=new_edges,
    )

    # Place the existing condition node (keeps its cases intact).
    cond_node = by_id[cond_id]
    cond_node["position"] = {"x": pre_end_x, "y": _LAYOUT_ROW_Y}
    new_nodes.append(cond_node)
    new_edges.append(_mk_edge(pre_last, cond_id,
                              source_handle=None if pre_chain else "body"))
    branch_x_base = pre_end_x + _LAYOUT_COL_W

    # First declared case → YES branch; remaining agents → ELSE branch.
    # Matches the seed convention where the case is the "exception" path
    # and ELSE is the happy path.
    cases = (cond_node.get("data") or {}).get("cases") or []
    first_case_id = (cases[0].get("id") if cases else "case-1") or "case-1"
    if len(branch_agent_ids) == 1:
        branches = {first_case_id: [branch_agent_ids[0]], "else": []}
    else:
        mid = len(branch_agent_ids) // 2
        branches = {
            first_case_id: branch_agent_ids[:mid] or branch_agent_ids[:1],
            "else":        branch_agent_ids[mid:] or branch_agent_ids[-1:],
        }

    branch_end_ids: List[str] = []
    max_branch_x = branch_x_base
    for row_idx, (handle, ids) in enumerate(branches.items()):
        if not ids:
            continue
        row_y = _LAYOUT_BRANCH_Y_TOP if row_idx == 0 else _LAYOUT_BRANCH_Y_BOTTOM
        next_x, last = _walk_chain(
            by_id, ids,
            start_x=branch_x_base, y=row_y,
            prev=cond_id, prev_handle=handle,
            new_nodes=new_nodes, new_edges=new_edges,
        )
        branch_end_ids.append(last)
        if next_x > max_branch_x:
            max_branch_x = next_x

    new_nodes.append(_mk_end(max_branch_x + 80))
    for bid in branch_end_ids:
        new_edges.append(_mk_edge(bid, loop_id))
    new_edges.append(_mk_edge(loop_id, "end", source_handle="exit"))

    graph["nodes"] = new_nodes
    graph["edges"] = new_edges


def _rewire_as_sequential(template: dict) -> None:
    """Rebuild a former loop template as a plain chain with NO loop node.

    Reuses the loop body registry (`_LOOP_BODY`) so the agent nodes and
    their order are preserved; only the surrounding start/end nodes, the
    edges and per-node positions change. Simple loops become
    `start → agent1 → … → agentN → end`. `loop_conditional` shapes keep
    their `condition` node and branch agents inline (start → pre… →
    condition → [yes/else branches] → end) — the branch tails now
    terminate at `end` instead of looping back."""
    tpl_id = template["id"]
    body_ids = _LOOP_BODY.get(tpl_id)
    if not body_ids:
        return

    graph = template["graph_data"]
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    missing = [aid for aid in body_ids if aid not in by_id]
    if missing:
        logger.warning(f'[AGENT] Sequential rebuild for {tpl_id} skipped — missing agent ids: {missing}')
        return

    chain_start_x = 250
    is_cond = (_PATTERN_OVERRIDES.get(tpl_id) == "conditional")

    new_nodes: List[dict] = [_mk_start()]
    new_edges: List[dict] = []

    if not is_cond:
        end_x, last = _walk_chain(
            by_id, list(body_ids),
            start_x=chain_start_x, y=_LAYOUT_ROW_Y,
            prev="start", prev_handle=None,
            new_nodes=new_nodes, new_edges=new_edges,
        )
        new_nodes.append(_mk_end(end_x))
        new_edges.append(_mk_edge(last, "end"))
        graph["nodes"] = new_nodes
        graph["edges"] = new_edges
        return

    # conditional shape:
    #   pre_agents… → condition → [yes_branch…, no_branch…] → end
    cond_id: Optional[str] = None
    pre_chain: List[str] = []
    branch_agent_ids: List[str] = []
    for aid in body_ids:
        n = by_id.get(aid)
        if n is None:
            continue
        if n.get("type") == "condition":
            cond_id = aid
        elif cond_id is None:
            pre_chain.append(aid)
        else:
            branch_agent_ids.append(aid)

    if cond_id is None or not branch_agent_ids:
        logger.warning(f'[AGENT] conditional rebuild for {tpl_id} skipped — no condition or branches found')
        return

    pre_end_x, pre_last = _walk_chain(
        by_id, pre_chain,
        start_x=chain_start_x, y=_LAYOUT_ROW_Y,
        prev="start", prev_handle=None,
        new_nodes=new_nodes, new_edges=new_edges,
    )

    cond_node = by_id[cond_id]
    cond_node["position"] = {"x": pre_end_x, "y": _LAYOUT_ROW_Y}
    new_nodes.append(cond_node)
    new_edges.append(_mk_edge(pre_last, cond_id))
    branch_x_base = pre_end_x + _LAYOUT_COL_W

    cases = (cond_node.get("data") or {}).get("cases") or []
    first_case_id = (cases[0].get("id") if cases else "case-1") or "case-1"
    if len(branch_agent_ids) == 1:
        branches = {first_case_id: [branch_agent_ids[0]], "else": []}
    else:
        mid = len(branch_agent_ids) // 2
        branches = {
            first_case_id: branch_agent_ids[:mid] or branch_agent_ids[:1],
            "else":        branch_agent_ids[mid:] or branch_agent_ids[-1:],
        }

    branch_end_ids: List[str] = []
    max_branch_x = branch_x_base
    for row_idx, (handle, ids) in enumerate(branches.items()):
        if not ids:
            continue
        row_y = _LAYOUT_BRANCH_Y_TOP if row_idx == 0 else _LAYOUT_BRANCH_Y_BOTTOM
        next_x, last = _walk_chain(
            by_id, ids,
            start_x=branch_x_base, y=row_y,
            prev=cond_id, prev_handle=handle,
            new_nodes=new_nodes, new_edges=new_edges,
        )
        branch_end_ids.append(last)
        if next_x > max_branch_x:
            max_branch_x = next_x

    new_nodes.append(_mk_end(max_branch_x + 80))
    for bid in branch_end_ids:
        new_edges.append(_mk_edge(bid, "end"))

    graph["nodes"] = new_nodes
    graph["edges"] = new_edges


def _rewire_as_parallel(template: dict) -> None:
    """Fan-out from start into the branch agents and join them on the
    downstream agent. The registry only contains multi-branch specs."""
    tpl_id = template["id"]
    spec = _PARALLEL_SPECS.get(tpl_id)
    if not spec:
        return
    branches: List[str] = spec.get("branches") or []
    join_id: str = spec.get("join") or ""
    if len(branches) < 2 or not join_id:
        logger.warning(f'[AGENT] Parallel rebuild for {tpl_id} skipped — needs ≥2 branches, got {len(branches)}')
        return

    graph = template["graph_data"]
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    missing = [aid for aid in branches + [join_id] if aid not in by_id]
    if missing:
        logger.warning(f'[AGENT] Parallel rebuild for {tpl_id} skipped — missing agent ids: {missing}')
        return

    new_nodes: List[dict] = [_mk_start()]
    new_edges: List[dict] = []

    branch_x = 280
    branch_y_step = 360 // (len(branches) - 1) if len(branches) > 1 else 0
    for i, bid in enumerate(branches):
        node = by_id[bid]
        node["position"] = {"x": branch_x, "y": 80 + i * branch_y_step}
        new_nodes.append(node)
        new_edges.append(_mk_edge("start", bid))

    join_x = branch_x + _LAYOUT_COL_W
    join_node = by_id[join_id]
    join_node["position"] = {"x": join_x, "y": _LAYOUT_ROW_Y}
    new_nodes.append(join_node)
    for bid in branches:
        new_edges.append(_mk_edge(bid, join_id))

    # Capture any agents that the original chain placed AFTER the join
    # (e.g. a third agent for translate→qa→export). They stay in order
    # on a single downstream row.
    edges = graph.get("edges", [])
    extra_chain: List[str] = []
    cursor = join_id
    visited = {join_id} | set(branches)
    while True:
        next_targets = [e["target"] for e in edges
                        if e.get("source") == cursor
                        and e.get("target") not in visited
                        and by_id.get(e["target"], {}).get("type") == "agent"]
        if not next_targets:
            break
        nxt = next_targets[0]
        visited.add(nxt)
        extra_chain.append(nxt)
        cursor = nxt

    end_x, prev = _walk_chain(
        by_id, extra_chain,
        start_x=join_x + _LAYOUT_COL_W, y=_LAYOUT_ROW_Y,
        prev=join_id, prev_handle=None,
        new_nodes=new_nodes, new_edges=new_edges,
    )
    new_nodes.append(_mk_end(end_x))
    new_edges.append(_mk_edge(prev, "end"))

    graph["nodes"] = new_nodes
    graph["edges"] = new_edges


def _apply_pattern_rebuild(template: dict) -> None:
    """Top-level pattern reclassification pass for one seed template:
       1. Stamp `pattern` from `_PATTERN_OVERRIDES`.
       2. Layer extra HITL gates from `_PATTERN_HITL_OVERRIDES` onto
          agent `data.hitlMode` if the agent currently has `off`.
       3. Restructure `graph_data` when the target pattern is loop /
          loop_conditional / parallel (and the spec has the right inputs).
    """
    tpl_id = template.get("id") or ""
    pattern = _PATTERN_OVERRIDES.get(tpl_id)
    if pattern:
        if pattern not in _VALID_PATTERNS:
            logger.warning(f'[AGENT] Unknown pattern {pattern!r} for template {tpl_id} — defaulting to sequential')
            pattern = "sequential"
        template["pattern"] = pattern

    # Apply structural rebuilds BEFORE the HITL overlay so HITL writes
    # the chosen mode onto the post-rebuild agent dicts (which are the
    # same objects — we mutate in place).
    if pattern in ("loop", "loop_conditional"):
        _rewire_as_loop(template)
    elif pattern in ("parallel", "parallel_conditional"):
        _rewire_as_parallel(template)
    elif tpl_id in _LOOP_BODY:
        # Former loop templates: strip the loop node and rebuild as a
        # plain sequential/conditional chain. Loops can be re-added
        # manually in the canvas editor if needed.
        _rewire_as_sequential(template)

    # Layer HITL gates from the override table (additive — never
    # downgrades an existing non-off mode). Skip the per-node walk when
    # this template has no overrides registered.
    tpl_hitl = _PATTERN_HITL_BY_TPL.get(tpl_id)
    if not tpl_hitl:
        return
    for node in (template.get("graph_data") or {}).get("nodes") or []:
        if node.get("type") != "agent":
            continue
        mode = tpl_hitl.get(node.get("id"))
        if mode is None:
            continue
        data = node.setdefault("data", {})
        if data.get("hitlMode", "off") in ("", "off"):
            data["hitlMode"] = mode


for _tpl in _SEED_TEMPLATES:
    _apply_pattern_rebuild(_tpl)


# ===========================================================================
# Template-normalisation post-pass
# ===========================================================================
# The seed templates were authored over time and drifted on a few fields the
# native execution engine reads (`app/engine/native_engine.py` +
# `app/services/services.py`). This post-pass fills the gaps so every template
# is consistent without rewriting every literal dict above:
#
#   1. Every agent node gets a complete `data` block (provider, apiKey,
#      modelName, baseUrl, temperature, maxTokens, topP, tools, skills,
#      hitlMode). The engine's `_extract_llm_config` already falls back to
#      env defaults when fields are empty, but normalising here keeps the
#      DB rows self-describing and the UI editor pre-populated.
#
#   2. Condition / loop nodes get a renderable `label`, mirroring the same
#      fix applied to MCP nodes — react-flow needs `data.label` to draw the
#      node header instead of an empty rectangle on the canvas.
#
#   3. Curated HITL gates: a small whitelist of decision-style agents
#      (security block, release notes, MR review, decline letters, exec
#      reports, learning plans) are flipped from "off" to a sensible HITL
#      mode so the canvas visibly demonstrates the human-in-the-loop
#      feature out-of-the-box.
#
#   4. Catalog tools: agents that currently ship with `tools: []` get a
#      hand-picked canonical toolset that matches their instructions, so
#      the "instant" templates aren't crippled the moment they're cloned.
#      Engineering templates already attach the right gitlab_*/jira_* tools
#      (verified in tests), so this pass only adds tools to non-engineering
#      agents that were genuinely toolless.
#
# All operations are idempotent: re-applying never overwrites a non-empty
# value the template author already chose.
# ---------------------------------------------------------------------------

# Default LLM/sampling values applied when an agent omits the field. Matches
# the fallbacks in `app/engine/native_engine.py::_extract_llm_config` so the
# canvas-rendered values agree with what the engine would resolve at runtime.
_AGENT_DEFAULTS: Dict[str, Any] = {
    "provider":    "custom",
    "apiKey":      "",
    "modelName":   "",
    "baseUrl":     "",
    "temperature": 0.3,
    "maxTokens":   8192,
    "topP":        1.0,
    "tools":       [],
    "skills":      [],
    "hitlMode":    "off",
    # Canonical KB blob — the legacy ``{enabled, namespaces}`` default was a
    # no-op (no ``mode`` key meant RAG was silently skipped). See KB_MODE_NONE.
    "knowledge":   dict(_KB_DEFAULT_BLOB),
}

# Agents that are natural human-review gates. Mode reference:
#   "after_response" — pause once the agent produces its answer
#   "before_tool"    — pause before any destructive tool call
#   "both"           — pause at both points
# Keyed by (template_id, agent_node_id) so updates stay surgical.
# NOTE: Human-in-the-loop gates have been removed from all templates. This
# table is intentionally empty so the post-pass applies no HITL modes.
# Users can re-enable HITL per agent manually in the canvas editor.
_HITL_GATES: Dict[tuple, str] = {}

# Per-agent tool attachments for agents that currently ship without any
# tools. Tool names come from `app/tools/canonical_tools.py` (verified by
# the seeding pass at startup). Each entry is keyed (template_id, agent_id)
# and the value is a list of (tool_name, human_description) tuples. The
# normaliser appends these without dropping any existing tools.
_AGENT_TOOL_BACKFILL: Dict[tuple, List[tuple]] = {
    # UC-58: Routers should be able to (re)assign to the on-call/standard
    # team alongside the transition + comment they already issue.
    ("template-support-ticket-triage", "escalation-router"): [
        ("jira_assign", "Assign the ticket to the on-call owner"),
    ],
    ("template-support-ticket-triage", "standard-router"): [
        ("jira_assign", "Assign the ticket to the standard-queue owner"),
    ],
}


# ---------------------------------------------------------------------------
# Per-agent tool REMOVAL (mismatch + over-attachment cleanup)
# ---------------------------------------------------------------------------
# Some seed templates accumulated tools that don't fit the agent's job
# (calendar tools on a transcript distiller, jira_create_issue on a social-
# media copy writer, etc). The audit below trims those down to the minimal
# accurate set. Removal runs AFTER the MCP attach pass and AFTER the
# backfill, so the final state on disk reflects exactly what the agent's
# instructions describe — no aspirational extras.
# ---------------------------------------------------------------------------
_AGENT_TOOL_REMOVE: Dict[tuple, List[str]] = {
    # UC-59: KB Responder drafts replies grounded in the KB — it doesn't
    # mutate Jira; the supervising flow can do that on top.
    ("template-kb-grounded-response-drafting", "kb-responder"): [
        "jira_get_issue",
        "jira_add_comment",
    ],
    # UC-63: Slot Proposer drafts the invite email; Jira is unrelated.
    ("template-interview-scheduling", "slot-proposer"): [
        "jira_create_issue",
        "jira_add_comment",
    ],
    # UC-64: Sequence Drafter writes the email — it shouldn't open Jira
    # tickets.
    ("template-candidate-follow-up-sequences", "sequence-drafter"): [
        "jira_create_issue",
    ],
    # UC-79: Copy Drafter writes social-media copy; opening Jira tickets
    # is a leftover from a generic scaffold.
    ("template-social-media-scheduling", "copy-drafter"): [
        "jira_create_issue",
    ],
    # UC-93: Response Drafter writes the response document; the
    # jira_create_issue used to track an internal review is fine on the
    # gating template, but on the instant tier it just adds clutter.
    ("template-rfp-response-drafting", "response-drafter"): [
        "jira_create_issue",
    ],
    # UC-95: SOP Drafter authors the SOP; the 4-eyes approval is the
    # responsibility of the human reviewer, not a stray jira_create_issue
    # from the agent.
    ("template-policy-sop-drafting", "sop-drafter"): [
        "jira_create_issue",
    ],
}

_DELETED_TOOL_PREFIXES = (
    "kb_search__",
    "document_tools__",
    "calendar_tools__",
    "email_tools__",
    "task_tracker__",
    "data_tools__",
    "ats_tools__",
    "doc_generator__",
    "translator__",
    "lms_tools__",
)


def _remove_tools(node_data: Dict[str, Any], remove_names: List[str]) -> None:
    """Drop tools whose `name` appears in `remove_names`. Idempotent.

    Logs nothing — missing names are fine (the entry might already be gone
    from an earlier run or the tool was never attached on this agent).
    """
    if not remove_names:
        return
    drop = set(remove_names)
    tools = node_data.get("tools") or []
    node_data["tools"] = [t for t in tools if (t.get("name") or "") not in drop]


def _remove_deleted_tools(node_data: Dict[str, Any]) -> None:
    tools = node_data.get("tools") or []
    node_data["tools"] = [
        t for t in tools
        if not (t.get("name") or "").startswith(_DELETED_TOOL_PREFIXES)
    ]


# Human-friendly labels for condition / loop nodes. The canvas renderer
# falls back to "Condition"/"Loop" when `data.label` is absent, but we keep
# the labels here so the UI shows the *intent* of the gate.
_CONDITION_LABEL_BY_ID: Dict[tuple, str] = {
    ("template-appsec-mr-scan", "severity-check"): "Critical findings?",
    ("template-brand-sentiment-monitoring", "cond"): "Negative sentiment spike?",
    ("template-incident-rca", "severity-check"): "Sev1 / Sev2?",
    ("template-risk-assessment", "risk-gate"): "High residual risk?",
}

_CONDITION_CASE_BACKFILL_BY_ID: Dict[tuple, Dict[str, Any]] = {
    ("template-brand-sentiment-monitoring", "cond", "case-1"): {
        "label": "Negative spike",
        "logic": "AND",
        "conditions": [
            {
                "id": "cond-has-negative-spike",
                "field": "has_negative_spike",
                "operator": "==",
                "value": True,
                "type": "boolean",
            }
        ],
    },
}


def _normalise_agent_node(node: Dict[str, Any]) -> None:
    """Fill missing default fields on a single agent node, in place."""
    data = node.setdefault("data", {})
    for key, default in _AGENT_DEFAULTS.items():
        if key not in data:
            # `default` is a plain value (str/float/list/dict) — copy
            # containers so two agents don't share the same list reference.
            if isinstance(default, (list, dict)):
                data[key] = type(default)(default)
            else:
                data[key] = default


def _attach_tools(node_data: Dict[str, Any], extra: List[tuple]) -> None:
    """Append `(name, description)` pairs to `data.tools`, skipping
    anything already present by name. Idempotent."""
    existing = {(t.get("name") or "").strip() for t in (node_data.get("tools") or [])}
    for name, desc in extra:
        if name in existing:
            continue
        node_data.setdefault("tools", []).append(
            {"name": name, "description": desc}
        )
        existing.add(name)


# Legacy seed templates authored condition cases as a bare
# ``{"id", "name", "expression"}`` triple. The backend evaluator still
# understands that (see services.build_expression_from_case), but the UI
# condition editor (frontend .../conditions/factories.js, ConditionCase.jsx)
# only renders the structured shape: a `conditions` list of
# ``{id, field, operator, value, type}`` rows plus a `logic` connector. Cases
# lacking that structure show up blank / "not defined" in the canvas. We
# reconstruct the structured rows from the legacy expression so every existing
# template opens correctly in the UI while producing an identical evaluation.

# ``input.<field> <op> <literal>`` where the literal is a number, True/False,
# or a quoted / bare string.
_EXPR_BINARY_RE = re.compile(
    r"^\s*input\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?P<op>==|!=|>=|<=|>|<)\s*(?P<val>.+?)\s*$"
)
# ``input.<field> in ['a','b',...]`` / ``not in [...]``.
_EXPR_IN_RE = re.compile(
    r"^\s*input\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?P<neg>not\s+)?in\s*\[(?P<items>.*)\]\s*$"
)


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
        return token[1:-1].replace("\\'", "'").replace('\\"', '"')
    return token


def _condition_rows_from_expression(expr: str) -> List[Dict[str, Any]]:
    """Parse a legacy expression string into structured condition rows.

    Returns ``[]`` when the expression cannot be parsed, letting the caller
    fall back to leaving the case as-is (the evaluator still handles it)."""
    if not expr or not isinstance(expr, str):
        return []

    # `in [...]` list membership → one equality row per list item so the UI
    # renders each candidate as an editable row (mirrors "priority in
    # ['P1','P2']"). `not in` flips == to != ; the case `logic` (OR for `in`,
    # AND for `not in`) reproduces the original semantics when re-joined.
    m = _EXPR_IN_RE.match(expr)
    if m:
        field = m.group("field")
        operator = "!=" if m.group("neg") else "=="
        items = [
            _unquote(part) for part in m.group("items").split(",") if part.strip()
        ]
        return [
            {
                "id": f"cond-{field}-{i}",
                "field": field,
                "operator": operator,
                "value": item,
                "type": "string",
            }
            for i, item in enumerate(items)
        ]

    m = _EXPR_BINARY_RE.match(expr)
    if m:
        field, op, raw = m.group("field"), m.group("op"), m.group("val").strip()
        low = raw.lower()
        if low in ("true", "false"):
            value: Any = low == "true"
            vtype = "boolean"
        elif re.fullmatch(r"-?\d+(\.\d+)?", raw):
            value = float(raw) if "." in raw else int(raw)
            vtype = "number"
        else:
            value = _unquote(raw)
            vtype = "string"
        return [
            {
                "id": f"cond-{field}",
                "field": field,
                "operator": op,
                "value": value,
                "type": vtype,
            }
        ]

    return []


def _normalise_condition_node(template_id: str, node: Dict[str, Any]) -> None:
    """Make sure condition nodes carry a renderable label and structured cases.

    Backfills, in order of precedence:
      1. an explicit per-case backfill (``_CONDITION_CASE_BACKFILL_BY_ID``),
      2. structured `conditions` rows derived from a legacy `expression`,
    and ensures every case has a `label` and `logic` so the UI editor can
    render it. Idempotent — cases already carrying `conditions` are untouched.
    """
    data = node.setdefault("data", {})
    cases = data.setdefault("cases", [])
    if "label" not in data:
        key = (template_id, node.get("id"))
        data["label"] = _CONDITION_LABEL_BY_ID.get(key, "Condition")
    for case in cases:
        case_key = (template_id, node.get("id"), case.get("id"))
        backfill = _CONDITION_CASE_BACKFILL_BY_ID.get(case_key)
        if backfill:
            case.update({k: v for k, v in backfill.items() if k not in case})

        # A renderable label — fall back to the legacy `name`, then a default.
        if not case.get("label"):
            case["label"] = case.get("name") or "Match"

        # Rebuild structured rows from the legacy expression when the UI-facing
        # `conditions` list is missing or empty.
        if not case.get("conditions"):
            rows = _condition_rows_from_expression(case.get("expression", ""))
            if rows:
                case["conditions"] = rows
                # A multi-row expansion comes from an `in [...]` / `not in [...]`
                # membership test: `in` is satisfied by ANY candidate (OR),
                # `not in` requires ALL to differ (AND). Single-row cases have
                # no connector, so the value is moot.
                multi_or = len(rows) > 1 and rows[0].get("operator") == "=="
                case.setdefault("logic", "OR" if multi_or else "AND")

        case.setdefault("logic", "AND")


def _normalise_loop_node(node: Dict[str, Any]) -> None:
    """Loop nodes are seeded with `mode` + `cases`; backfill a label only."""
    data = node.setdefault("data", {})
    data.setdefault("label", "Loop")
    data.setdefault("cases", [])
    data.setdefault("mode", "while")


def _apply_post_pass_to_template(template: Dict[str, Any]) -> None:
    tpl_id = template.get("id") or ""
    graph  = template.get("graph_data") or {}
    nodes  = graph.get("nodes") or []

    for node in nodes:
        ntype = node.get("type")
        if ntype == "agent":
            _normalise_agent_node(node)
            data = node["data"]
            # Tool backfill for agents that shipped without any tools.
            key = (tpl_id, node.get("id"))
            if key in _AGENT_TOOL_BACKFILL:
                _attach_tools(data, _AGENT_TOOL_BACKFILL[key])
            # Tool REMOVAL — trim mismatches / over-attachments. Runs
            # AFTER the backfill so removing then re-adding produces a
            # stable final state.
            if key in _AGENT_TOOL_REMOVE:
                _remove_tools(data, _AGENT_TOOL_REMOVE[key])
            _remove_deleted_tools(data)
            # HITL gates.
            if key in _HITL_GATES and data.get("hitlMode", "off") == "off":
                data["hitlMode"] = _HITL_GATES[key]
        elif ntype == "condition":
            _normalise_condition_node(tpl_id, node)
        elif ntype == "loop":
            _normalise_loop_node(node)
        elif ntype in ("start", "end"):
            # Already shipped with `data.label`, but be defensive.
            data = node.setdefault("data", {})
            data.setdefault("label", "Start" if ntype == "start" else "End")

    # `hitl` is always derived from the final agent config (post HITL
    # overlay), so the DB column never lies about the canvas state.
    template.setdefault("pattern", "sequential")
    template["hitl"] = any(
        (n.get("data") or {}).get("hitlMode", "off") not in ("", "off")
        for n in nodes
        if n.get("type") == "agent"
    )


for _tpl in _SEED_TEMPLATES:
    _apply_post_pass_to_template(_tpl)


# ---------------------------------------------------------------------------
# Viable-32 description rewrite
# ---------------------------------------------------------------------------
# The original Viable-32 descriptions were copied off the spec deck and read
# like checklist items rather than catalog copy ("Compute X, flag Y, explain
# Z..."). They also picked up two competing tag prefixes -- the seed literal
# carries "[UC-nn | Viable-32 | <tier>]" and the MCP post-pass prepends a
# "[MCP: ...]" marker -- which together pushed the actual sentence past the
# truncation cliff on the templates card.
#
# The map below holds outcome-focused, single-sentence descriptions for each
# of the 32 UCs. The pass re-builds the final description as:
#     "[UC-nn | Viable-32 | <tier>] <narrative>"
# preserving the searchable tag while replacing only the narrative. The MCP
# pass already ran before this, so any "[MCP: ...]" marker that was injected
# is stripped here and re-applied in a normalised position so the prefix
# block always reads `[UC | MCP] narrative.`.
# ---------------------------------------------------------------------------

_VIABLE_32_DESCRIPTIONS: Dict[int, str] = {
    57:  ("Distil a meeting transcript into a structured summary and turn every "
           "commitment into a tracked action item with owner and due date."),
    58:  ("Triage incoming support tickets by intent and priority, then route "
           "P1/P2s to the on-call queue and P3/P4s to the standard queue with a "
           "rationale comment."),
    59:  ("Draft support replies strictly grounded in the knowledge base, with "
           "per-claim citations and an explicit escalation when the docs don't "
           "cover the question."),
    60:  ("Auto-resolve tickets that match a known routine pattern after "
           "verifying preconditions, and escalate everything else with full "
           "context so the human agent doesn't start from zero."),
    62:  ("Score a resume against the JD's must-haves with evidence-backed "
           "rationale, then either prepare a shortlist brief or draft a "
           "courteous decline (HITL gate before sending)."),
    63:  ("Find common interview slots across the candidate's availability and "
           "the panel's calendars, then draft the invite email with the top "
           "three options."),
    64:  ("Segment the candidate pipeline by stage-age rules and draft "
           "personalised follow-up sequences, queued as drafts for recruiter "
           "review (never auto-sent)."),
    65:  ("Build the new-hire onboarding checklist from the start date, "
           "orchestrate each task as a tracked ticket, and report day-zero "
           "readiness to the hiring manager."),
    66:  ("Answer employee HR questions strictly from the HR policy corpus, "
           "citing document and section, and explicitly refuse to extrapolate "
           "when the policy is silent."),
    67:  ("Extract invoice fields, validate against the purchase order with "
           "tolerance checks, and either route exceptions to AP or prepare a "
           "posting-ready summary for approval."),
    68:  ("Categorise expense lines against the T&E policy, flag every "
           "violation with the rule and overage, or recommend approval when "
           "the report is clean."),
    69:  ("Reconcile bank transactions to the ledger with fuzzy matching, "
           "propose correcting entries for discrepancies (proposals only), and "
           "certify a clean close when matched."),
    70:  ("Compute budget-vs-actual variances per department, flag breaches at "
           "5% and escalate at 15%, then explain drivers in a "
           "management-ready report (HITL gate on the narrative)."),
    71:  ("Compute MoM deltas and opex ratios from the monthly financials, "
           "then write a board-ready management report with executive "
           "summary, breakdown table, and outlook (HITL gate before send)."),
    72:  ("Extract contract clauses, compare to the internal contracting "
           "standard, and produce either a Do-Not-Sign risk memo for critical "
           "findings or an advisory summary with negotiation asks."),
    73:  ("Summarise long legal documents in plain language with explicit "
           "obligations, deadlines, and red flags called out by party."),
    74:  ("Map evidence documents to compliance checklist items, judge "
           "sufficiency, then deliver a gap matrix with remediation owners or "
           "an attestation when all controls are evidenced."),
    77:  ("Adapt one source asset into channel-native drafts (LinkedIn, X, "
           "newsletter, video script) within each channel's exact constraints "
           "and brand voice."),
    79:  ("Plan a posting calendar within platform constraints and draft "
           "platform-native copy for each slot, scheduled only as drafts."),
    82:  ("Draft a press release from a brief in standard PR structure with "
           "dateline, lead paragraph, boilerplate, and quote slots for named "
           "approvers."),
    83:  ("Design a methodologically-sound questionnaire from a research goal, "
           "then analyse pilot responses into themes, scores, and an action "
           "list."),
    84:  ("Cluster customer feedback verbatims into themes by underlying need, "
           "quantify each, and produce a Voice-of-Customer report ranking top "
           "pains and praise."),
    86:  ("Triage an executive inbox against the exec's stated priorities, "
           "draft replies for urgent items, and produce a same-day digest of "
           "what needs the exec's attention (HITL on outgoing drafts)."),
    87:  ("Resolve scheduling conflicts against working-hours and protected-"
           "block rules, propose times, and draft responses to incoming "
           "meeting requests."),
    90:  ("Answer an analytical question from tabular data with computed "
           "evidence, supporting charts, and explicit caveats about data "
           "quality and confidence."),
    91:  ("Compile KPIs vs targets from the monthly metrics workbook and "
           "assemble a steering-committee deck with trends, risks, and asks "
           "(HITL gate before distribution)."),
    93:  ("Extract every RFP question, map to the content library, and draft "
           "evidence-backed answers that follow the issuer's submission rules "
           "and flag library gaps honestly."),
    94:  ("Translate documents per locale enforcing the glossary strictly, "
           "then QA terminology and formatting against the source before "
           "publishing the localised copy."),
    95:  ("Restructure raw process notes into a structured SOP (Purpose, "
           "Scope, Roles, Procedure, Exceptions) and stage it for a 4-eyes "
           "approval before publishing."),
    96:  ("Design a course from learning objectives and the source material, "
           "then build the learner workbook, slides, and assessment grounded "
           "in those sources."),
    97:  ("Score account health against churn-risk signals, draft "
           "personalised outreach for the top at-risk accounts, or deliver a "
           "clean-portfolio report when no thresholds are breached."),
    100: ("Assess a learner's level and goals against the catalog, then build "
           "an adaptive learning plan with weekly milestones and demonstrable "
           "checkpoints (HITL gate on the final plan)."),
}


def _rebuild_viable_32_description(template: Dict[str, Any]) -> None:
    """Replace the narrative portion of a Viable-32 template's description
    with the curated copy from `_VIABLE_32_DESCRIPTIONS`, preserving only
    the `[UC-nn | Viable-32 | <tier>]` searchable prefix.

    The MCP server marker (`[MCP: ...]`) is intentionally dropped — MCP
    tools are now inlined as catalog tools on the agent card, so the
    marker was leaking into the catalog UI as the leading text on each
    template card. The same dependency information is still discoverable
    through the agent's tool chips.
    """
    uc = template.get("usecase_uc")
    if uc is None or uc not in _VIABLE_32_DESCRIPTIONS:
        return

    tier = template.get("execution_tier") or "instant"
    uc_tag = f"[UC-{uc} | Viable-32 | {tier} tier]"
    narrative = _VIABLE_32_DESCRIPTIONS[uc]
    template["description"] = f"{uc_tag} {narrative}"


for _tpl in _SEED_TEMPLATES:
    _rebuild_viable_32_description(_tpl)


# ---------------------------------------------------------------------------
# Classify every workflow template as "Engineering" or "Non-Engineering".
# Engineering  = software-delivery flows (SDLC / AppSec / dev issue + MR
#                automation that act on GitLab code and Jira dev tickets).
# Non-Engineering = HR, finance, legal, sales, marketing, CS, research, and
#                executive-ops flows -- including the 50 catalog presets above.
# The label is written onto each template dict as `classification`.
# ---------------------------------------------------------------------------
_ENGINEERING_TEMPLATE_IDS = {
    "template-sdlc-feature-flow",
    "template-sdlc-bug-flow",
    "template-code-review",
    "template-appsec-mr-scan",
    "template-jira-issue-triage",
    "template-gitlab-mr-to-jira",
    "template-release-notes-generator",
}

for _template in _SEED_TEMPLATES:
    _template["classification"] = (
        "Engineering"
        if _template["id"] in _ENGINEERING_TEMPLATE_IDS
        else "Non-Engineering"
    )


# ---------------------------------------------------------------------------
# THE VIABLE 32: stamp every catalog use-case template with its UC number and
# execution tier, and tag descriptions so the 32 are identifiable everywhere.
# ---------------------------------------------------------------------------
INSTANT_TIER_UC_IDS = frozenset({
    57, 59, 62, 63, 66, 70, 71, 72, 73, 74, 82, 83, 84, 86, 87, 90, 91, 93,
    94, 95, 96, 100,
})
GATED_TIER_UC_IDS = frozenset({58, 60, 64, 65, 67, 68, 69, 77, 79, 97})
VIABLE_32_UC_IDS = INSTANT_TIER_UC_IDS | GATED_TIER_UC_IDS
# The 18 web-search-dependent use cases have been removed from the catalog
# entirely, so this set is now empty. Kept as a public constant so any
# external importers keep working.
EXCLUDED_WEB_SEARCH_UC_IDS = frozenset()
assert len(INSTANT_TIER_UC_IDS) == 22 and len(GATED_TIER_UC_IDS) == 10
assert len(VIABLE_32_UC_IDS) == 32

# Template-ids hidden from the catalog. The web-search-dependent workflows
# that populated this set have been deleted from `_SEED_TEMPLATES`, so it is
# now empty. Derived from `_NON_ENGINEERING_USECASES` so it repopulates
# automatically if any `excluded_web_search` specs are ever added back.
HIDDEN_TEMPLATE_IDS = frozenset({
    "template-" + _spec["slug"]
    for _spec in _NON_ENGINEERING_USECASES
    if _spec.get("tier") == "excluded_web_search"
})


# ---------------------------------------------------------------------------
# Seed persistence — direct edits to this module's source
# ---------------------------------------------------------------------------
# "Save to seed" and "Create new template" rewrite the literal
# `_SEED_TEMPLATES` list inside this very file. New entries are appended
# at the bottom of the initial seed block (just before its closing `]`)
# and existing entries are replaced in place when their `id` matches.
# That way the catalog under version control reflects exactly what the
# template editor produces — no parallel sidecar JSON to keep in sync.
#
# Removal recipe: drop the appended `# ── EDITOR-MANAGED ── …` block
# from `_SEED_TEMPLATES` to revert.
_SOURCE_PATH = os.path.abspath(__file__)
_EDITOR_MARK = "# EDITOR-MANAGED"
# Anchor the search/append region to the literal start of the primary
# `_SEED_TEMPLATES = [` block so unrelated `]` lines and stray `"id":`
# strings (in instructions, descriptions, node labels) can never be
# mistaken for a list boundary.
_SEED_BLOCK_HEADER = "_SEED_TEMPLATES = [\n"

# Serialize "Save to seed" / "Create new template" writes so two
# concurrent requests can't lose each other's update via interleaved
# read-modify-write on the source file.
_SEED_WRITE_LOCK = threading.Lock()


def _format_seed_entry_source(entry: Dict[str, Any]) -> str:
    body = pprint.pformat(entry, indent=4, width=100, sort_dicts=False)
    indented = textwrap.indent(body, "    ")
    return f"    {_EDITOR_MARK} {entry['id']}\n{indented},\n"


def _seed_block_bounds(source: str) -> Tuple[int, int]:
    """Return `(body_start, terminator_pos)` of the primary
    `_SEED_TEMPLATES = [ ... ]` block. `body_start` points just after
    the `[\\n` header; `terminator_pos` points at the line containing
    the closing `]\\n`. Raises `LookupError` if the markers are missing
    or unbalanced."""
    header = source.find(_SEED_BLOCK_HEADER)
    if header == -1:
        raise LookupError(
            f"_SEED_TEMPLATES header not found in {_SOURCE_PATH}"
        )
    body_start = header + len(_SEED_BLOCK_HEADER)
    # Brace-balanced scan from body_start to find the matching `]` of the
    # list literal. We're already past the opening `[`, so depth starts
    # at 1 and we track `[]` (the list) AND `{}` (entries) to skip past
    # any nested lists inside `graph_data`.
    depth_sq = 1
    depth_cu = 0
    i = body_start
    in_str = False
    str_quote = ""
    escape = False
    while i < len(source):
        ch = source[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == str_quote:
                in_str = False
        elif ch in ('"', "'"):
            in_str = True
            str_quote = ch
        elif ch == "{":
            depth_cu += 1
        elif ch == "}":
            depth_cu -= 1
        elif ch == "[":
            depth_sq += 1
        elif ch == "]":
            depth_sq -= 1
            if depth_sq == 0 and depth_cu == 0:
                # Snap back to the start of the line so the closing `]`
                # is replaced cleanly when we splice.
                line_start = source.rfind("\n", body_start, i) + 1
                return body_start, line_start
        i += 1
    raise LookupError(
        f"_SEED_TEMPLATES list never closed in {_SOURCE_PATH}"
    )


def _find_entry_span(
    source: str, body_start: int, body_end: int, template_id: str,
) -> Optional[Tuple[int, int]]:
    """Locate the `(start, end)` char span of the entry whose top-level
    `"id"` equals `template_id`, scanning ONLY within the primary seed
    block. Returns `None` if no match. The scan walks top-level entries
    in order and inspects each entry's FIRST `"id"` key at depth 1 — so
    a colliding `"id": "..."` substring inside a nested instruction
    body, description, or graph node never derails the match."""
    needle_json = f'"id": "{template_id}"'
    needle_py = f"'id': '{template_id}'"
    i = body_start
    in_str = False
    str_quote = ""
    escape = False
    entry_start: Optional[int] = None
    depth = 0
    id_line_end: Optional[int] = None
    while i < body_end:
        ch = source[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == str_quote:
                in_str = False
        elif ch in ('"', "'"):
            in_str = True
            str_quote = ch
        elif ch == "{":
            if depth == 0:
                entry_start = source.rfind("\n", body_start, i) + 1
                # Find the end of the entry's first non-blank line after
                # the opening `{`; the `id` key — if present — lives on
                # that line for both hand-written and pprint-rendered
                # entries. Inspecting just this line, not the full body,
                # is what guarantees nested `"id": "..."` strings can't
                # be mistaken for the top-level field.
                first_nl = source.find("\n", i)
                second_nl = source.find("\n", first_nl + 1) if first_nl != -1 else -1
                id_line_end = second_nl if second_nl != -1 else (first_nl if first_nl != -1 else i + 1)
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and entry_start is not None and id_line_end is not None:
                head = source[entry_start:id_line_end]
                if needle_json in head or needle_py in head:
                    j = i + 1
                    if j < body_end and source[j] == ",":
                        j += 1
                    if j < body_end and source[j] == "\n":
                        j += 1
                    return entry_start, j
                entry_start = None
                id_line_end = None
        i += 1
    return None


def _upsert_seed_entry(entry: Dict[str, Any]) -> None:
    """Patch this module's source so `_SEED_TEMPLATES` reflects `entry`,
    then update the in-memory caches so the running process sees the
    change without a restart. Holds `_SEED_WRITE_LOCK` to guarantee
    read-modify-write atomicity across concurrent saves; the on-disk
    swap itself uses `os.replace` for crash safety."""
    with _SEED_WRITE_LOCK:
        # Skip the file write entirely if the in-memory seed already
        # matches the incoming entry — saves a ~700KB rewrite, mtime
        # bump, git churn, and autoreload trigger on no-op saves.
        for existing in _SEED_TEMPLATES:
            if existing.get("id") == entry["id"]:
                if existing == entry:
                    return
                break

        rendered = _format_seed_entry_source(entry)
        with open(_SOURCE_PATH, "r", encoding="utf-8") as fh:
            source = fh.read()

        body_start, body_end = _seed_block_bounds(source)
        span = _find_entry_span(source, body_start, body_end, entry["id"])

        if span is not None:
            new_source = source[:span[0]] + rendered + source[span[1]:]
        else:
            new_source = source[:body_end] + rendered + source[body_end:]

        tmp = _SOURCE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new_source)
            # fsync so the new contents survive a crash between
            # `write` and `os.replace`'s rename.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _SOURCE_PATH)

        for i, seed in enumerate(_SEED_TEMPLATES):
            if seed.get("id") == entry["id"]:
                _SEED_TEMPLATES[i] = entry
                break
        else:
            _SEED_TEMPLATES.append(entry)
        _TEMPLATES_BY_ID[entry["id"]] = entry


_TEMPLATES_BY_ID = {_t["id"]: _t for _t in _SEED_TEMPLATES}
for _uc_spec in _NON_ENGINEERING_USECASES:
    # Both the Viable-32 and the 18 web-search-dependent UCs are now seeded
    # into _SEED_TEMPLATES, so every spec should resolve to a template here.
    # Guard with .get() anyway to stay robust if a slug is ever removed.
    _t = _TEMPLATES_BY_ID.get("template-" + _uc_spec["slug"])
    if _t is None:
        continue
    _t["usecase_uc"]     = _uc_spec["uc"]
    _t["execution_tier"] = _uc_spec["tier"]
    _t["viable_32"]      = _uc_spec["uc"] in VIABLE_32_UC_IDS
    if _t["viable_32"]:
        _tag = "[UC-%d | Viable-32 | %s tier] " % (_uc_spec["uc"], _uc_spec["tier"])
    else:
        _tag = "[UC-%d | EXCLUDED - requires web search] " % _uc_spec["uc"]
    if not _t["description"].startswith("[UC-"):
        _t["description"] = _tag + _t["description"]


def get_viable_32_usecase_templates() -> List[Dict[str, Any]]:
    """Seed template dicts for THE VIABLE 32 non-engineering use cases
    (instant + gated tiers; excludes the 18 web-search-dependent ones)."""
    return [t for t in _SEED_TEMPLATES if t.get("viable_32")]


def get_usecase_template(uc: int) -> Optional[Dict[str, Any]]:
    """Look up a catalog use-case seed template by UC number (51-100)."""
    for t in _SEED_TEMPLATES:
        if t.get("usecase_uc") == uc:
            return t
    return None


# ---------------------------------------------------------------------------
# Agent template seed data (pre-built agent presets served via API)
# ---------------------------------------------------------------------------
_SEED_AGENT_TEMPLATES = [
    # ── SDLC ──────────────────────────────────────────────────────────────
    {
        "id": "preset-tech-lead",
        "name": "Tech Lead",
        "description": "Reads a Jira feature ticket and produces a detailed design document with trade-off analysis",
        "category": "SDLC",
        "instructions": (
            "You are a senior tech lead. When given a Jira issue key:\n"
            "1. Fetch the full ticket (summary, description, acceptance criteria).\n"
            "2. Produce a design document covering: approach, files to change, "
            "API contracts, data model changes, edge cases, and trade-offs.\n"
            "3. Produce the design document as a Word document (.docx) using the "
            "attached skill so it is shareable, and post a concise summary with the "
            "key decisions as a Jira comment for stakeholder review.\n"
            "Always cite the Jira ticket key and keep the tone collaborative. Ground "
            "every statement in the ticket -- never invent requirements or "
            "constraints. Ask only for missing inputs."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.4, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
        ],
        "skills": [
            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
        ],
    },
    {
        "id": "preset-senior-dev",
        "name": "Senior Developer",
        "description": "Implements features by creating GitLab branches, writing code, and opening merge requests linked to Jira",
        "category": "SDLC",
        "instructions": (
            "You are a senior developer. Given a Jira ticket key and a design plan:\n"
            "1. Create a feature branch in GitLab from main "
            "(branch name: feature/<jira-key>-<short-slug>).\n"
            "2. Implement the required changes by creating or updating files.\n"
            "3. Open a merge request with the Jira key in the title and a summary of "
            "changes in the description.\n"
            "4. Post the MR link as a Jira comment.\n"
            "Follow clean code principles, write meaningful commit messages, and keep "
            "changes focused."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.3, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
            {"name": "gitlab_create_branch", "description": "Create a new branch in a GitLab repo"},
            {"name": "gitlab_create_or_update_file", "description": "Create or update a file in a GitLab repo"},
            {"name": "gitlab_create_mr", "description": "Open a merge request in GitLab"},
            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
        ],
        "skills": [],
    },
    {
        "id": "preset-bug-triager",
        "name": "Bug Triager",
        "description": "Classifies bugs by severity, identifies root cause, and updates the Jira ticket with triage notes",
        "category": "SDLC",
        "instructions": (
            "You are a senior QA engineer responsible for bug triage. When given a Jira "
            "bug ticket key:\n"
            "1. Fetch the full issue details.\n"
            "2. Analyse the reported bug and classify severity: P1-Critical, P2-High, "
            "P3-Medium, P4-Low.\n"
            "3. Identify the likely root cause and affected components.\n"
            "4. Update the Jira ticket with your triage notes: severity, root-cause "
            "hypothesis, reproduction steps assessment, and recommended fix approach.\n"
            "5. Transition the issue to 'Triaged' status.\n"
            "Be precise and avoid speculation -- if you're uncertain, say so."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.3, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
            {"name": "jira_update_issue", "description": "Update fields on a Jira issue"},
            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
            {"name": "jira_transition_issue", "description": "Transition a Jira issue to a new status"},
        ],
        "skills": [],
    },
    {
        "id": "preset-code-reviewer",
        "name": "Code Reviewer",
        "description": "Fetches a GitLab merge request diff and posts a thorough, constructive code review",
        "category": "SDLC",
        "instructions": (
            "You are an expert code reviewer. Given a GitLab repository and MR IID:\n"
            "1. Fetch the merge request details and all changed files.\n"
            "2. Review every change for: bugs, security issues, performance problems, "
            "style violations, missing edge cases, and test coverage gaps.\n"
            "3. Post a detailed review with actionable, constructive feedback.\n"
            "Always suggest improvements rather than just pointing out problems. Cite "
            "specific lines where possible. Keep a calm, respectful tone -- never "
            "accuse, always advise."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.4, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "gitlab_get_mr", "description": "Get merge request details"},
            {"name": "gitlab_get_mr_files", "description": "Get changed files in a merge request"},
            {"name": "gitlab_create_mr_review", "description": "Post a review on a merge request"},
        ],
        "skills": [],
    },
    # ── AppSec ────────────────────────────────────────────────────────────
    {
        "id": "preset-appsec-reviewer",
        "name": "AppSec Reviewer",
        "description": "Scans GitLab MR changes for security vulnerabilities, secrets, and OWASP Top-10 issues",
        "category": "AppSec",
        "instructions": (
            "You are an application security engineer. Given a GitLab repository and "
            "MR IID:\n"
            "1. Fetch the MR details and all changed files.\n"
            "2. Analyse every change for: hardcoded secrets or API keys, SQL injection, "
            "XSS, insecure dependencies, unsafe deserialization, path traversal, and "
            "other OWASP Top-10 issues.\n"
            "3. For each finding, report: severity (Critical/High/Medium/Low), affected "
            "file and line, description of the issue, and a concrete remediation "
            "recommendation.\n"
            "4. If critical findings exist, post a blocking review. Otherwise post an "
            "advisory comment or approval.\n"
            "Be thorough but avoid false positives -- explain your reasoning for each "
            "finding."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.2, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "gitlab_get_mr", "description": "Get merge request details"},
            {"name": "gitlab_get_mr_files", "description": "Get changed files in a merge request"},
            {"name": "gitlab_create_mr_review", "description": "Post a review on a merge request"},
            {"name": "gitlab_comment_on_mr", "description": "Add a comment on a merge request"},
        ],
        "skills": [],
    },
    # ── Incident / SRE ────────────────────────────────────────────────────
    {
        "id": "preset-incident-commander",
        "name": "Incident Commander",
        "description": "Correlates signals from Jira, proposes root-cause hypotheses, and creates a P1 incident ticket",
        "category": "Incident",
        "instructions": (
            "You are a war-room incident commander. When an incident is reported:\n"
            "1. Gather context: list related open issues for the impacted service, "
            "and fetch the details of any referenced ticket to reconstruct what has "
            "happened so far.\n"
            "2. Correlate signals and propose up to 3 root-cause hypotheses ranked "
            "by likelihood.\n"
            "3. Create a P1 Jira issue in the SRE project with: summary, affected "
            "service, timeline of events, hypotheses, and recommended immediate "
            "actions.\n"
            "4. Post a succinct status update as a comment.\n"
            "Be concise under pressure -- bullet points over paragraphs. Always "
            "include a timeline. Ground hypotheses in the observed signals -- never "
            "invent events or metrics."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.3, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "jira_list_issues", "description": "List Jira issues by project and status"},
            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
            {"name": "jira_create_issue", "description": "Create a new Jira issue"},
            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
        ],
        "skills": [],
    },
    {
        "id": "preset-rca-writer",
        "name": "RCA Writer",
        "description": "Generates a blameless root cause analysis from a Jira incident ticket using the 5-whys method",
        "category": "Incident",
        "instructions": (
            "You are a blameless retrospective author. Given a Jira incident ticket "
            "key:\n"
            "1. Fetch the full issue details including all comments (timeline of the "
            "incident).\n"
            "2. Write a structured RCA document with: Executive Summary, Timeline of "
            "Events, 5-Whys Analysis, Root Cause, Contributing Factors, Action Items "
            "(with owners and due dates), and Lessons Learned.\n"
            "3. Post the RCA as a Jira comment on the incident ticket.\n"
            "Maintain a blameless tone throughout -- focus on systems and processes, "
            "never on individuals. The goal is to learn and prevent recurrence."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.4, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
        ],
        "skills": [],
    },
    # ── Operations ────────────────────────────────────────────────────────
    {
        "id": "preset-issue-triager",
        "name": "Issue Triager",
        "description": "Automatically classifies, prioritises, labels, and assigns incoming Jira issues",
        "category": "Operations",
        "instructions": (
            "You are a senior project manager responsible for triaging incoming Jira "
            "issues. Given an issue key:\n"
            "1. Fetch the full issue details.\n"
            "2. Classify the priority (P1-Critical through P4-Low) based on impact "
            "and urgency.\n"
            "3. Determine the appropriate team or assignee based on the component or "
            "area described.\n"
            "4. Add relevant labels (e.g., bug, feature, infra, security, ux).\n"
            "5. Update the issue with priority, assignee, and labels.\n"
            "6. Add a triage comment explaining your classification rationale and "
            "recommended next steps.\n"
            "7. Transition the issue from 'Open' to 'Triaged' (or 'To Do')."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.3, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
            {"name": "jira_update_issue", "description": "Update fields on a Jira issue"},
            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
            {"name": "jira_transition_issue", "description": "Transition a Jira issue to a new status"},
        ],
        "skills": [],
    },
    {
        "id": "preset-mr-jira-linker",
        "name": "MR-Jira Linker",
        "description": "Automatically links GitLab merge requests to their referenced Jira tickets with comments on both sides",
        "category": "Operations",
        "instructions": (
            "You are a DevOps automation agent. Given a GitLab repo and MR IID:\n"
            "1. Fetch the MR details (title, description, author, source branch).\n"
            "2. Extract the Jira issue key from the MR title or branch name "
            "(e.g., PROJ-123).\n"
            "3. Link the MR to the Jira issue.\n"
            "4. Add a comment on the Jira ticket with the MR URL, title, author, "
            "and current status.\n"
            "5. If no Jira key is found, post a comment on the MR asking the author "
            "to include one in the branch name or title."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.2, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "gitlab_get_mr", "description": "Get merge request details"},
            {"name": "gitlab_link_mr_to_jira", "description": "Link a GitLab MR to a Jira issue"},
            {"name": "gitlab_comment_on_mr", "description": "Add a comment on a merge request"},
            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
        ],
        "skills": [],
    },
    {
        "id": "preset-release-notes",
        "name": "Release Notes Generator",
        "description": "Collects merged GitLab MRs and resolved Jira issues to generate structured release notes",
        "category": "Operations",
        "instructions": (
            "You are a release engineer. When asked to generate release notes:\n"
            "1. List all recently merged MRs from the target GitLab repo.\n"
            "2. List recently resolved Jira issues in the project.\n"
            "3. Correlate MRs with Jira issues by matching ticket keys in MR titles "
            "and branch names.\n"
            "4. Generate well-structured release notes with sections: New Features, "
            "Bug Fixes, Improvements, and Breaking Changes.\n"
            "5. Each entry should reference the Jira key and MR number.\n"
            "6. Create a Jira issue of type 'Release' with the formatted release "
            "notes in the description.\n"
            "Ground every entry in the actual merged MRs and resolved issues -- "
            "never invent changes, tickets, or MR numbers. If an MR has no matching "
            "Jira key, list it under an 'Unlinked changes' section rather than "
            "guessing a ticket."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.5, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "gitlab_list_mrs", "description": "List merge requests in a GitLab repo"},
            {"name": "jira_list_issues", "description": "List Jira issues by project and status"},
            {"name": "jira_create_issue", "description": "Create a new Jira issue"},
        ],
        "skills": [],
    },
    # ── Compliance ────────────────────────────────────────────────────────
    {
        "id": "preset-contract-risk-reviewer",
        "name": "Contract Risk Reviewer",
        "description": "Reviews a contract against an internal standard, flags risky clauses by severity, and produces a Do-Not-Sign risk memo or an advisory summary",
        "category": "Compliance",
        "instructions": (
            "You are a contracts counsel reviewing an agreement against the "
            "internal contracting standard provided in the inputs. Given the "
            "contract text (and the standard/playbook when supplied):\n"
            "1. Extract the material clauses: liability, indemnity, termination, "
            "IP ownership, data protection, payment terms, and auto-renewal.\n"
            "2. Compare each clause to the standard and classify it: Compliant, "
            "Negotiable, or Critical (deviates from a non-negotiable term).\n"
            "3. For every Critical or Negotiable clause, quote the contract text, "
            "quote the standard, and state the specific redline ask.\n"
            "4. If any Critical findings exist, produce a Do-Not-Sign risk memo "
            "ranked by severity with the counterparty deadline if stated. "
            "Otherwise produce an advisory summary with the highest-value "
            "negotiation asks called out first.\n"
            "Produce the final deliverable as a Word document (.docx) using the "
            "attached skill. Ground every statement in the inputs -- never invent "
            "clauses, figures, or obligations. Ask only for missing business "
            "inputs (the contract, the standard)."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.3, "max_tokens": 8192, "top_p": 1.0,
        "tools": [],
        "skills": [
            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
        ],
    },
    # ── Finance ───────────────────────────────────────────────────────────
    {
        "id": "preset-invoice-validator",
        "name": "Invoice Validator",
        "description": "Validates invoice line items against a purchase order with tolerance checks, flags exceptions, and produces a posting-ready check matrix",
        "category": "Finance",
        "instructions": (
            "You are an accounts-payable analyst. Given an invoice and the "
            "matching purchase order (PO) in the inputs:\n"
            "1. Extract invoice header and line items (vendor, PO number, dates, "
            "quantities, unit prices, tax, totals).\n"
            "2. Three-way match each line against the PO: vendor identity, "
            "quantity (allow the stated tolerance, default 0), unit price, and "
            "total. Mark each check PASS or FAIL.\n"
            "3. Classify each failure as a blocker (vendor/price mismatch) or a "
            "tolerable variance (minor quantity under-delivery), with the reason.\n"
            "4. If any blocking exceptions exist, recommend routing to AP for "
            "resolution; otherwise mark the invoice posting-ready.\n"
            "Produce the final deliverable as an Excel spreadsheet (.xlsx) -- the "
            "full check matrix with invoice-vs-PO values side by side and a "
            "recommendation cell -- using the attached skill. Do NOT post the "
            "invoice yourself. Ground every value in the inputs; never invent "
            "figures. Ask only for missing business inputs (invoice, PO)."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.2, "max_tokens": 8192, "top_p": 1.0,
        "tools": [],
        "skills": [
            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
        ],
    },
    # ── HR ────────────────────────────────────────────────────────────────
    {
        "id": "preset-jd-writer",
        "name": "JD Writer",
        "description": "Drafts a precise, role-specific job description from your inputs, asking only for what's missing, and exports it as a Word or PDF document",
        "category": "HR",
        "instructions": (
            "You are an experienced talent-acquisition partner. Given the role "
            "brief (title, team, level, key responsibilities, must-have and "
            "nice-to-have skills, location, employment type):\n"
            "1. Draft a structured job description with these sections: About the "
            "Role, Responsibilities, Required Qualifications, Preferred "
            "Qualifications, and What We Offer.\n"
            "2. Keep language inclusive and free of biased or exclusionary "
            "wording; avoid unrealistic 'unicorn' requirement lists.\n"
            "3. Clearly separate must-haves from nice-to-haves.\n"
            "4. If a critical input is missing (e.g. level or key "
            "responsibilities), ask for it rather than inventing it.\n"
            "Produce the final deliverable as a Word document (.docx) -- and a "
            "PDF when requested -- using the attached skills. Ground every "
            "statement in the provided brief; never fabricate compensation, "
            "benefits, or company facts that were not supplied."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.4, "max_tokens": 8192, "top_p": 1.0,
        "tools": [],
        "skills": [
            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
            {"name": "pdf", "description": "Generate, fill, split, and merge PDF documents"},
        ],
    },
    # ── Support ───────────────────────────────────────────────────────────
    {
        "id": "preset-support-ticket-triager",
        "name": "Support Ticket Triager",
        "description": "Classifies a support ticket by intent and priority, then routes it to the right queue with a rationale comment on the tracking issue",
        "category": "Operations",
        "instructions": (
            "You are a support operations specialist triaging an incoming "
            "ticket. Given a Jira issue key for the ticket:\n"
            "1. Fetch the full issue details.\n"
            "2. Classify the intent (e.g. access request, bug, how-to, billing, "
            "outage) and the priority P1-P4 from customer impact and urgency.\n"
            "3. Decide the routing: P1/P2 go to the on-call queue, P3/P4 to the "
            "standard queue. State the target team.\n"
            "4. Update the issue with the priority and appropriate labels.\n"
            "5. Add a triage comment explaining the intent, priority rationale, "
            "the routing decision, and the first thing the assignee should check. "
            "For P1/P2, clearly flag that the SLA clock is running.\n"
            "Be factual and concise. Do not promise resolutions -- your job is "
            "accurate classification and routing, not fixing the issue."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.3, "max_tokens": 8192, "top_p": 1.0,
        "tools": [
            {"name": "jira_get_issue", "description": "Fetch full Jira issue details by key"},
            {"name": "jira_update_issue", "description": "Update fields on a Jira issue"},
            {"name": "jira_add_comment", "description": "Add a comment to a Jira issue"},
        ],
        "skills": [],
    },
    {
        "id": "preset-meeting-notes-summarizer",
        "name": "Meeting Notes Summarizer",
        "description": "Turns raw meeting notes or a transcript into a structured summary with decisions and tracked action items, exported as a shareable document",
        "category": "Operations",
        "instructions": (
            "You are an executive assistant. Given raw meeting notes or a "
            "transcript:\n"
            "1. Produce a concise summary of what was discussed.\n"
            "2. Extract the key decisions made, each on its own line.\n"
            "3. Extract every action item as a tracked entry with owner and due "
            "date; if an owner or date is not stated, mark it TBD rather than "
            "guessing.\n"
            "4. Note any open questions or items explicitly deferred.\n"
            "Produce the final deliverable as a Word document (.docx) -- Summary, "
            "Decisions, Action Items (owner / due date), and Open Questions -- "
            "using the attached skill. Ground everything in the provided notes; "
            "never invent commitments, names, or dates that were not mentioned."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.3, "max_tokens": 8192, "top_p": 1.0,
        "tools": [],
        "skills": [
            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
        ],
    },
    # ── Reporting ─────────────────────────────────────────────────────────
    {
        "id": "preset-report-writer",
        "name": "Report Writer",
        "description": "Turns figures and findings into a board-ready management report with an executive summary, a supporting data table, and an outlook",
        "category": "Research & Exec",
        "instructions": (
            "You are a management reporting analyst. Given the period's figures "
            "and any supporting context in the inputs:\n"
            "1. Compute the headline metrics and period-over-period deltas that "
            "the report should lead with.\n"
            "2. Write an executive summary (3-5 sentences) stating what changed "
            "and why it matters.\n"
            "3. Build a breakdown table of the key metrics with current value, "
            "prior value, delta, and a one-line driver for each material change.\n"
            "4. Close with a short, evidence-based outlook and any risks or asks "
            "for the reader.\n"
            "Produce the final deliverable as a Word document (.docx) with an "
            "embedded summary table, and produce the supporting figures as an "
            "Excel spreadsheet (.xlsx) when the numbers warrant it -- using the "
            "attached skills. Ground every number and claim in the inputs; never "
            "fabricate figures. Ask only for missing business inputs."
        ),
        "provider": "custom", "model_name": "",
        "temperature": 0.4, "max_tokens": 8192, "top_p": 1.0,
        "tools": [],
        "skills": [
            {"name": "docx", "description": "Create and edit professional Word documents (.docx)"},
            {"name": "xlsx", "description": "Create and analyse Excel spreadsheets (.xlsx) with formulas and charts"},
        ],
    },
]
 
 
# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
 
async def init_db() -> None:
    """Bind to the shared platform pool, create tables, seed templates."""
    global _pool
    if not postgres_enabled():
        logger.warning('[AGENT] POSTGRES_HOST not set  -- workflow persistence disabled')
        return
 
    def _run():
        global _pool
        # Reuse the platform's single shared connection pool (the SQLAlchemy
        # engine in db.database) instead of opening a separate psycopg pool.
        # The whole AiNxt process therefore holds ONE set of Postgres
        # connections. Pool sizing now lives in db/database.py; the legacy
        # WORKFLOW_PG_POOL_* env vars are no longer used.
        from app.core.db_pool import SHARED_POOL
        _pool = SHARED_POOL
        with _pool.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflows (
                    id            TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    description   TEXT NOT NULL DEFAULT '',
                    author        TEXT NOT NULL DEFAULT 'User',
                    graph_data    JSONB NOT NULL,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    owner_user_id TEXT
                )
            """)
            conn.execute("""
                ALTER TABLE workflows
                ADD COLUMN IF NOT EXISTS owner_user_id TEXT
            """)
            conn.execute("""
                ALTER TABLE workflows
                ADD COLUMN IF NOT EXISTS source_template_id TEXT
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_workflows_source_template
                ON workflows (owner_user_id, source_template_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_workflows_owner_user_id
                ON workflows (owner_user_id)
            """)
            # Workflow-level knowledge attachment. Inherited by every agent
            # node whose own knowledge blob has ``mode == 'none'`` — the
            # runtime fallback lives in ``native_engine._run_agent``.
            conn.execute(
                "ALTER TABLE workflows "
                "ADD COLUMN IF NOT EXISTS knowledge JSONB NOT NULL DEFAULT '{\"mode\": \"none\"}'"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS templates (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    category    TEXT NOT NULL DEFAULT 'general',
                    graph_data  JSONB NOT NULL,
                    pattern     TEXT NOT NULL DEFAULT 'sequential',
                    hitl        BOOLEAN NOT NULL DEFAULT FALSE,
                    visibility  TEXT NOT NULL DEFAULT 'public',
                    department  TEXT
                )
            """)
            # Pattern + HITL facet — added post-launch so existing installs
            # need an ALTER. The columns drive the catalog UI's pattern
            # filter chip and the HITL badge. Defaults match the legacy
            # behaviour (every old row reads as a plain sequential, no
            # human-in-the-loop) so this migration is invisible to anything
            # that doesn't query the new fields.
            conn.execute(
                "ALTER TABLE templates "
                "ADD COLUMN IF NOT EXISTS pattern TEXT NOT NULL DEFAULT 'sequential'"
            )
            conn.execute(
                "ALTER TABLE templates "
                "ADD COLUMN IF NOT EXISTS hitl BOOLEAN NOT NULL DEFAULT FALSE"
            )
            # Department-scoped visibility (Deploy-to-templates feature). A
            # template is 'public' (all users) or 'private' (only users whose
            # department matches ``department``). Seed/built-in templates default
            # to 'public' so they stay visible to everyone. ``department`` is the
            # publishing user's department, set when a Deploy request is approved.
            conn.execute(
                "ALTER TABLE templates "
                "ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'public'"
            )
            conn.execute(
                "ALTER TABLE templates ADD COLUMN IF NOT EXISTS department TEXT"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    id            TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    description   TEXT NOT NULL DEFAULT '',
                    instructions  TEXT NOT NULL DEFAULT '',
                    provider      TEXT NOT NULL DEFAULT 'custom',
                    model_name    TEXT NOT NULL DEFAULT '',
                    api_key       TEXT NOT NULL DEFAULT '',
                    temperature   FLOAT NOT NULL DEFAULT 0.7,
                    max_tokens    INT NOT NULL DEFAULT 8192,
                    top_p         FLOAT NOT NULL DEFAULT 1.0,
                    base_url      TEXT NOT NULL DEFAULT '',
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    owner_user_id TEXT
                )
            """)
            conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS tools  JSONB NOT NULL DEFAULT '[]'")
            conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS skills JSONB NOT NULL DEFAULT '[]'")
            conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS guardrails    JSONB NOT NULL DEFAULT '{}'")
            conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS memory_config JSONB NOT NULL DEFAULT '{}'")
            # Knowledge attachment for Build Studio agents. See
            # app/core/kb_retriever.py for the shape and ACL contract.
            conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS knowledge JSONB NOT NULL DEFAULT '{\"mode\": \"none\"}'")
            # Attached flows — per-agent list of downstream workflows
            # that run after the agent emits its final answer. Persisted
            # as JSONB array of attachment descriptors (see
            # ``app/agent_factory/pipeline.py::_run_attached_flows`` for
            # the shape). This column only stores user-edited workflow
            # refs from the Build Studio AgentEditor. Dynamic delegation
            # is handled at runtime by the adaptive swarm
            # (``app.swarm``); there is no DB representation of
            # sub-agents.
            conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS attached_flows JSONB NOT NULL DEFAULT '[]'")
            # Per-agent swarm/subagents opt-in. Default FALSE (enterprise-safe):
            # when false the runtime does NOT inject spawn_swarm for this agent.
            # Mirrors the per-node ``enable_subagents`` pin on workflow agent
            # nodes, but persisted here because standalone agents have no graph.
            conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS use_subagents BOOLEAN NOT NULL DEFAULT FALSE")
            conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS source_template_id TEXT")
            # Optional per-agent "sample document" (look-and-feel reference)
            # attached from the Agent editor. The end user uploads any
            # existing document (.docx / .pptx / .xlsx / .pdf) they want
            # future outputs to resemble; the runtime exposes its path
            # via SAMPLE_DOC_PATH inside code_executor and appends a
            # prompt block instructing the LLM to treat it as guidance
            # (branding, styles, layouts) while remaining free to adapt
            # structure and content to the task. Empty {} when no sample
            # is attached. Shape:
            #     {
            #       "path":       "<abs path to persisted file>",
            #       "kind":       "docx" | "pptx" | "xlsx" | "pdf",
            #       "name":       "<original filename for display>",
            #       "size_bytes": <int>,
            #       "notes":      "<optional user guidance for the agent>",
            #       "uploaded_at": "<ISO-8601 UTC>"
            #     }
            conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS sample_doc JSONB NOT NULL DEFAULT '{}'")
            # The columns that briefly lived here in an earlier design
            # (``sub_agents``, ``callable_as_subagent``,
            # ``input_contract``, ``output_contract``,
            # ``auto_discover_subagents``) are dropped if they exist —
            # delegation is now expressed entirely at runtime via the
            # swarm orchestrator. DROPs are guarded with IF EXISTS so
            # fresh installs are unaffected.
            for _drop in (
                "sub_agents",
                "callable_as_subagent",
                "input_contract",
                "output_contract",
                "auto_discover_subagents",
            ):
                conn.execute(f"ALTER TABLE agents DROP COLUMN IF EXISTS {_drop}")
            conn.execute("DROP INDEX IF EXISTS idx_agents_callable_as_subagent")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_agents_owner_user_id
                ON agents (owner_user_id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tools_catalog (
                    name         TEXT PRIMARY KEY,
                    description  TEXT NOT NULL DEFAULT '',
                    input_schema JSONB NOT NULL DEFAULT '{}',
                    code         TEXT NOT NULL,
                    generated    BOOLEAN NOT NULL DEFAULT TRUE,
                    service      TEXT NOT NULL DEFAULT '',
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # service added post-launch; ALTER handles existing installs,
            # CREATE TABLE above already includes it for new installs.
            conn.execute("ALTER TABLE tools_catalog ADD COLUMN IF NOT EXISTS service TEXT NOT NULL DEFAULT ''")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skills_catalog (
                    name        TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    category    TEXT NOT NULL DEFAULT 'general',
                    content     TEXT NOT NULL,
                    generated   BOOLEAN NOT NULL DEFAULT TRUE,
                    -- Origin of the row: 'builtin' (shipped with the platform),
                    -- 'ai' (created via the Skill Factory), or 'upload' (a user
                    -- imported a packaged .zip/.skill bundle). Used by the
                    -- Skills tab to render distinct AI Generated / Uploaded
                    -- filters and badges. Added post-launch; the ALTER below
                    -- backfills existing rows for older installs.
                    source      TEXT NOT NULL DEFAULT 'ai',
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # ``source`` was added after skills_catalog first shipped. Preserve
            # existing rows by making the ALTER idempotent: builtin rows can be
            # identified by ``generated=false``; everything else defaults to
            # ``ai`` (correct for the pre-upload era when the only user-created
            # skills came from the AI Skill Factory). The uploaded-bundle path
            # will explicitly set source='upload' going forward.
            conn.execute("ALTER TABLE skills_catalog ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ai'")
            conn.execute("UPDATE skills_catalog SET source = 'builtin' WHERE generated = FALSE AND source = 'ai'")
            # Progressive-disclosure companion table: SKILL.md body stays in
            # skills_catalog.content, but every bundled file (reference docs
            # and scripts) lives here so the LLM can pull them on demand via
            # the read_skill_file tool instead of having them all jammed into
            # the system prompt every turn.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skill_files (
                    skill_name   TEXT NOT NULL REFERENCES skills_catalog(name) ON DELETE CASCADE,
                    rel_path     TEXT NOT NULL,
                    content      TEXT NOT NULL,
                    size_bytes   INTEGER NOT NULL,
                    description  TEXT NOT NULL DEFAULT '',
                    kind         TEXT NOT NULL,
                    abs_path     TEXT NOT NULL,
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (skill_name, rel_path)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS skill_files_by_skill "
                "ON skill_files(skill_name)"
            )
            # ---- Triggers (Routines) -----------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS triggers (
                    id            TEXT PRIMARY KEY,
                    target_kind   TEXT NOT NULL,
                    target_id     TEXT NOT NULL,
                    name          TEXT NOT NULL DEFAULT '',
                    schedule      JSONB NOT NULL,
                    input_text    TEXT NOT NULL DEFAULT '',
                    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
                    owner_user_id TEXT NOT NULL,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    next_run_at   TIMESTAMPTZ,
                    last_run_at   TIMESTAMPTZ,
                    last_status   TEXT
                )
            """)
            # node_id added in v2: a workflow trigger can optionally target
            # a specific agent node inside the workflow so the chain starts
            # there. Nullable for backwards compatibility — null means
            # "start from the workflow's Start node" (the original behaviour).
            conn.execute("ALTER TABLE triggers ADD COLUMN IF NOT EXISTS node_id TEXT")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_triggers_target
                ON triggers (target_kind, target_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_triggers_target_node
                ON triggers (target_kind, target_id, node_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_triggers_owner
                ON triggers (owner_user_id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trigger_executions (
                    id           BIGSERIAL PRIMARY KEY,
                    trigger_id   TEXT NOT NULL,
                    target_kind  TEXT NOT NULL,
                    target_id    TEXT NOT NULL,
                    target_name  TEXT NOT NULL DEFAULT '',
                    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at  TIMESTAMPTZ,
                    status       TEXT NOT NULL DEFAULT 'running',
                    input_text   TEXT NOT NULL DEFAULT '',
                    output       TEXT,
                    error        TEXT,
                    owner_user_id TEXT NOT NULL,
                    seen          BOOLEAN NOT NULL DEFAULT FALSE,
                    generated_files JSONB NOT NULL DEFAULT '[]'
                )
            """)
            # Back-fill the column on pre-existing tables (idempotent). Carries
            # the download references for documents produced by a triggered run
            # so the Inbox can render download chips (see finalize_trigger_execution).
            conn.execute(
                "ALTER TABLE trigger_executions "
                "ADD COLUMN IF NOT EXISTS generated_files JSONB NOT NULL DEFAULT '[]'"
            )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trigger_executions_trigger
                ON trigger_executions (trigger_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trigger_executions_owner_started
                ON trigger_executions (owner_user_id, started_at DESC)
            """)
            # Factory build sessions (agent / workflow / skill factory chat).
            # Previously these lived only in in-memory dicts in the factory
            # pipelines, so a half-finished "build me an agent/workflow/skill"
            # conversation was lost on backend restart or LRU eviction. We
            # mirror the whole session state to Postgres as a single JSONB
            # blob (matching the cowork_conversations recipe) so a build can
            # be resumed after a restart. Scoped by owner_user_id.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS factory_sessions (
                    id            TEXT NOT NULL,
                    factory_type  TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    state         JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (id, owner_user_id)
                )
            """)
            # Filter out the 18 web-search-dependent templates from the
            # seed payload — they stay in `_SEED_TEMPLATES` for catalog-
            # inspection helpers but are not surfaced to the UI.
            seedable = [t for t in _SEED_TEMPLATES
                        if t["id"] not in HIDDEN_TEMPLATE_IDS]
            count = conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
            if count == 0:
                seeded = 0
                for t in seedable:
                    try:
                        with conn.transaction():
                            conn.execute(
                                "INSERT INTO templates "
                                "(id, name, description, category, graph_data, pattern, hitl) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                (t["id"], t["name"], t["description"], t["category"],
                                 json.dumps(t["graph_data"]),
                                 t.get("pattern", "sequential"),
                                 bool(t.get("hitl", False))),
                            )
                        seeded += 1
                    except Exception as _e:
                        logger.error(f"[AGENT] Failed to seed template {t.get('id')}: {_e}")
                logger.info(f'[AGENT] Seeded {seeded}/{len(seedable)} default templates')
            else:
                # Purge any previously-seeded hidden templates so older
                # installs catch up to the current hide-list.
                try:
                    with conn.transaction():
                        purged = conn.execute(
                            "DELETE FROM templates WHERE id = ANY(%s)",
                            (list(HIDDEN_TEMPLATE_IDS),),
                        ).rowcount or 0
                    if purged:
                        logger.info(f'[AGENT] Purged {purged} hidden (web-search) templates from catalog')
                except Exception as _e:
                    logger.error(f'[AGENT] Failed to purge hidden templates: {_e}')

                # Insert any new seed templates that don't already exist.
                # Each insert runs in its own savepoint so one bad row can't
                # abort the rest of the batch (and so a later failure in this
                # init_db pass can't roll the inserts back).
                existing_ids = {
                    row[0]
                    for row in conn.execute("SELECT id FROM templates").fetchall()
                }
                content_repurposing_template = _TEMPLATES_BY_ID.get("template-content-repurposing")
                if content_repurposing_template and "template-content-repurposing" in existing_ids:
                    conn.execute(
                        "UPDATE templates SET category = %s WHERE id = %s",
                        (content_repurposing_template["category"], "template-content-repurposing"),
                    )

                inserted = 0
                failed = 0
                for t in seedable:
                    if t["id"] in existing_ids:
                        continue
                    try:
                        with conn.transaction():
                            conn.execute(
                                "INSERT INTO templates "
                                "(id, name, description, category, graph_data, pattern, hitl) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                (t["id"], t["name"], t["description"], t["category"],
                                 json.dumps(t["graph_data"]),
                                 t.get("pattern", "sequential"),
                                 bool(t.get("hitl", False))),
                            )
                        inserted += 1
                    except Exception as _e:
                        failed += 1
                        logger.error(f"[AGENT] Failed to seed template {t.get('id')}: {_e}")

                # Batch-refresh every code-owned seed template on restart so
                # deployments pick up changed graph_data/name/description/category
                # without requiring the templates table to be dropped manually.
                refresh_rows = [
                    (t["name"],
                     t["description"],
                     t["category"],
                     json.dumps(t["graph_data"]),
                     t.get("pattern", "sequential"),
                     bool(t.get("hitl", False)),
                     t["id"])
                    for t in seedable if t["id"] in existing_ids
                ]
                refreshed = 0
                if refresh_rows:
                    try:
                        with conn.transaction():
                            with conn.cursor() as cur:
                                cur.executemany(
                                    "UPDATE templates SET "
                                    "name = %s, description = %s, category = %s, "
                                    "graph_data = %s, pattern = %s, hitl = %s "
                                    "WHERE id = %s",
                                    refresh_rows,
                                )
                        refreshed = len(refresh_rows)
                    except Exception as _e:
                        logger.error(f'[AGENT] Failed to batch-refresh seed templates: {_e}')
                logger.info(f'[AGENT] Template seeding (incremental): {inserted} inserted, {refreshed} refreshed from code, {failed} failed, {len(existing_ids)} already present, {len(seedable)} total seedable')
            # Commit the templates seeding immediately so a later failure in
            # this init_db pass (e.g. agent_templates seeding) cannot roll
            # back the templates we just inserted.
            conn.commit()
            # ---- Agent Templates (pre-built agent presets) ----------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_templates (
                    id           TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    description  TEXT NOT NULL DEFAULT '',
                    category     TEXT NOT NULL DEFAULT 'general',
                    instructions TEXT NOT NULL DEFAULT '',
                    provider     TEXT NOT NULL DEFAULT 'custom',
                    model_name   TEXT NOT NULL DEFAULT '',
                    temperature  FLOAT NOT NULL DEFAULT 0.7,
                    max_tokens   INT NOT NULL DEFAULT 8192,
                    top_p        FLOAT NOT NULL DEFAULT 1.0,
                    tools        JSONB NOT NULL DEFAULT '[]',
                    skills       JSONB NOT NULL DEFAULT '[]',
                    visibility   TEXT NOT NULL DEFAULT 'public',
                    department   TEXT,
                    knowledge    JSONB NOT NULL DEFAULT '{"mode": "none"}',
                    use_subagents BOOLEAN NOT NULL DEFAULT FALSE,
                    source_agent_id TEXT
                )
            """)
            # Department-scoped visibility for agent presets (same contract as
            # the templates table above).
            conn.execute(
                "ALTER TABLE agent_templates "
                "ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'public'"
            )
            conn.execute(
                "ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS department TEXT"
            )
            # Knowledge-base attachment for agent presets. Without this column,
            # cloning a template into an agent (use_agent_template) always lost
            # the KB selection and the agent fell back to {"mode": "none"}.
            # Mirrors the agents.knowledge column; see app/core/kb_retriever.py
            # for the blob shape and ACL contract.
            conn.execute(
                "ALTER TABLE agent_templates "
                "ADD COLUMN IF NOT EXISTS knowledge JSONB NOT NULL DEFAULT '{\"mode\": \"none\"}'"
            )
            # Subagents/swarm delegation flag for agent presets. Without this
            # column, cloning a template into an agent (use_agent_template)
            # always reset the toggle to FALSE. Mirrors agents.use_subagents.
            conn.execute(
                "ALTER TABLE agent_templates "
                "ADD COLUMN IF NOT EXISTS use_subagents BOOLEAN NOT NULL DEFAULT FALSE"
            )
            # Provenance: the agent this preset was published from. Used by
            # use_agent_template to copy the source agent's triggers onto the
            # cloned agent. NULL for seeded presets (no originating agent).
            conn.execute(
                "ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS source_agent_id TEXT"
            )
            at_count = conn.execute("SELECT COUNT(*) FROM agent_templates").fetchone()[0]
            if at_count == 0:
                for at in _SEED_AGENT_TEMPLATES:
                    conn.execute(
                        "INSERT INTO agent_templates "
                        "(id, name, description, category, instructions, provider, "
                        " model_name, temperature, max_tokens, top_p, tools, skills) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (at["id"], at["name"], at["description"], at["category"],
                         at["instructions"], at["provider"], at["model_name"],
                         at["temperature"], at["max_tokens"], at["top_p"],
                         json.dumps(at["tools"]), json.dumps(at["skills"])),
                    )
                logger.info(f'[AGENT] Seeded {len(_SEED_AGENT_TEMPLATES)} default agent templates')
            conn.commit()
            # ────────────────────────────────────────────────────────────────
            # Governance mirror columns (approval layer, commit 93d31ff1)
            # ────────────────────────────────────────────────────────────────
            # The approval bridge stores template-provenance / approval-hash
            # columns on the platform's ``*_pg`` mirror tables (in the ``ainxt``
            # schema). Those columns normally ship via ``db/migrate.py``, but
            # that migration only runs as a deliberate deploy step
            # (RUN_MIGRATIONS_ON_STARTUP). On environments where it hasn't been
            # run, a governance submit fails with
            # ``column workflows_pg.source_template_id does not exist``. Since
            # ABStudio's init_db runs on every boot and already shares the same
            # database, we self-heal the columns here — schema-qualified to
            # ``ainxt`` so we touch the mirror tables (not ABStudio's own
            # ``public.workflows`` / ``public.agents``). Best-effort and in its
            # own savepoint: if the ``*_pg`` tables don't exist (standalone
            # ABStudio with no platform DB) we log and move on without breaking
            # the rest of init_db.
            _gov_cols = ("source_template_id VARCHAR(255)",
                         "source_template_hash VARCHAR(64)",
                         "last_approved_hash VARCHAR(64)")
            _gov_ok = 0
            _gov_err: Optional[str] = None
            from db.database import DB_SCHEMA as _db_schema
            for _pg_table in (f"{_db_schema}.agents_pg", f"{_db_schema}.skills_pg", f"{_db_schema}.workflows_pg"):
                for _col in _gov_cols:
                    try:
                        with conn.transaction():
                            conn.execute(
                                f"ALTER TABLE {_pg_table} ADD COLUMN IF NOT EXISTS {_col}"
                            )
                        _gov_ok += 1
                    except Exception as _ge:
                        # Log LOUD (warning) so a permission/ownership problem on
                        # the *_pg tables (e.g. the ABStudio DB user can SELECT
                        # but not ALTER them) is visible instead of silently
                        # leaving the governance submit broken.
                        _gov_err = f"{type(_ge).__name__}: {_ge}"
                        logger.warning(f'[AGENT] governance column ensure FAILED for {_pg_table} ({_col}): {_gov_err}')
            conn.commit()
            if _gov_err:
                logger.warning(f'[AGENT] governance mirror columns: {_gov_ok}/9 ensured; some ALTERs failed (last error: {_gov_err}). If this is a privilege error, the ABStudio DB user lacks ALTER on the ainxt.*_pg tables — grant it or run db/migrate.py as the schema owner.')
            else:
                logger.info(f'[AGENT] governance mirror columns ensured ({_gov_ok}/9)')
            # ────────────────────────────────────────────────────────────────
            # Loop Engineering tables (added in Phase 1)
            # ────────────────────────────────────────────────────────────────
            # See docs/loop-engineering/PHASE_1_FOUNDATIONS.md §4. Append-only
            # block — every statement uses ``CREATE TABLE IF NOT EXISTS`` or
            # ``ADD COLUMN IF NOT EXISTS`` so a redeploy is idempotent and
            # nothing here can roll back the templates / agents / agent_templates
            # work we just committed above. All writers live in
            # ``backend/app/loop/repo.py`` (P1) or the LoopRunner package
            # (P2 onward). Tables are created up-front so the schema is stable
            # before the runtime that consumes them ships.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS loops_pg (
                    id                  TEXT PRIMARY KEY,
                    name                TEXT NOT NULL,
                    org_id              TEXT NOT NULL DEFAULT 'default',
                    category            TEXT NOT NULL DEFAULT 'engineering',
                    description         TEXT,
                    trigger             JSONB NOT NULL DEFAULT '{}'::jsonb,
                    action              JSONB NOT NULL DEFAULT '{}'::jsonb,
                    proof               JSONB NOT NULL DEFAULT '[]'::jsonb,
                    memory              JSONB NOT NULL DEFAULT '{}'::jsonb,
                    stopping_condition  JSONB NOT NULL DEFAULT '{}'::jsonb,
                    isolation           JSONB NOT NULL DEFAULT '{}'::jsonb,
                    verify              JSONB NOT NULL DEFAULT '{}'::jsonb,
                    on_unresolved       JSONB NOT NULL DEFAULT '{"route_to":"triage_inbox"}'::jsonb,
                    version             TEXT NOT NULL DEFAULT '1.0.0',
                    status              TEXT NOT NULL DEFAULT 'DRAFT',
                    visibility          TEXT NOT NULL DEFAULT 'private',
                    department          TEXT,
                    owner_user_id       TEXT,
                    created_by          TEXT,
                    approved_by         TEXT,
                    approved_at         TIMESTAMPTZ,
                    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_loops_name_org UNIQUE (name, org_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS ix_loops_status ON loops_pg(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_loops_dept   ON loops_pg(department)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_loops_owner  ON loops_pg(owner_user_id)")

            # Append-only version history. Every create_loop / update_loop
            # writes one row; the loop_id+version pair is unique so a buggy
            # caller can't accidentally clobber an earlier snapshot.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS loop_versions (
                    id          BIGSERIAL PRIMARY KEY,
                    loop_id     TEXT NOT NULL,
                    version     TEXT NOT NULL,
                    snapshot    JSONB NOT NULL,
                    edited_by   TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_loop_versions UNIQUE (loop_id, version)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_loop_versions_loop ON loop_versions(loop_id)"
            )

            # First-class Goal objects (predicate + stop condition + budget).
            # CRUD ships in P1 so the schema is stable; the LoopRunner in
            # P2 consumes goals via /loops/{id}/run-stream and /run-stream.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS goals (
                    id              TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    description     TEXT,
                    predicate_kind  TEXT NOT NULL DEFAULT 'llm_judge',
                    predicate       JSONB NOT NULL DEFAULT '{}'::jsonb,
                    stop_condition  JSONB NOT NULL DEFAULT '{}'::jsonb,
                    owner_user_id   TEXT,
                    department      TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS ix_goals_owner ON goals(owner_user_id)")

            # Per-run audit (one row per LoopRunner.execute()). Writers ship
            # incrementally — append_event / record_budget / update_run are
            # defined in app/loop/repo.py from P1 onward so P2 plumbing has
            # no new table dependency.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS loop_runs (
                    id            TEXT PRIMARY KEY,
                    loop_id       TEXT,
                    goal_id       TEXT,
                    workflow_id   TEXT,
                    thread_id     TEXT,
                    trigger_src   TEXT,
                    worktree_ref  TEXT,
                    status        TEXT NOT NULL DEFAULT 'RUNNING',
                    iterations    INTEGER NOT NULL DEFAULT 0,
                    tokens_used   BIGINT NOT NULL DEFAULT 0,
                    wall_clock_s  DOUBLE PRECISION NOT NULL DEFAULT 0,
                    termination   TEXT,
                    outcome       JSONB NOT NULL DEFAULT '{}'::jsonb,
                    initial_score DOUBLE PRECISION,
                    final_score   DOUBLE PRECISION,
                    owner_user_id TEXT,
                    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    ended_at      TIMESTAMPTZ
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS ix_loop_runs_loop   ON loop_runs(loop_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_loop_runs_goal   ON loop_runs(goal_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_loop_runs_status ON loop_runs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_loop_runs_owner  ON loop_runs(owner_user_id)")

            # Per-iteration / proof / verifier / inbox / compliance events.
            # Distinct from the inner LoopNode's loop_iterations table by
            # design (different lifecycle, different shape — see D2).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS loop_run_events (
                    id          BIGSERIAL PRIMARY KEY,
                    run_id      TEXT NOT NULL,
                    seq         INTEGER NOT NULL,
                    kind        TEXT NOT NULL,
                    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_loop_events_run  ON loop_run_events(run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_loop_events_kind ON loop_run_events(kind)"
            )

            # Budget meter (NFR-L2). One row per metering tick; the
            # BudgetMeter writes whenever it observes a non-zero usage
            # delta or wall-clock advance, so the table can be queried for
            # post-mortem ``tokens-per-iteration`` trend lines.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS budget_ledger (
                    id            BIGSERIAL PRIMARY KEY,
                    run_id        TEXT NOT NULL,
                    tokens        BIGINT NOT NULL DEFAULT 0,
                    wall_clock_s  DOUBLE PRECISION NOT NULL DEFAULT 0,
                    cost_usd      DOUBLE PRECISION,
                    source        TEXT,
                    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_budget_ledger_run ON budget_ledger(run_id)"
            )

            # Reflections (verbalised lessons). Writer arrives in P5; the
            # table is here in P1 so the schema is stable and code paths
            # that read reflections in P5 don't introduce a new migration.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reflections (
                    id          TEXT PRIMARY KEY,
                    scope_kind  TEXT NOT NULL,
                    scope_id    TEXT NOT NULL,
                    tag         TEXT,
                    content     TEXT NOT NULL,
                    embedding   JSONB,
                    source_run  TEXT,
                    created_by  TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_reflections_scope ON reflections(scope_kind, scope_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_reflections_tag   ON reflections(tag)"
            )

            # Verification gate audit (P4 writer). Sibling of
            # condition_routings — separate table because the verifier runs
            # outside the per-node dispatcher and needs its own evidence_ref
            # + staged_diff_sha columns.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS verification_gate_runs (
                    id              BIGSERIAL PRIMARY KEY,
                    run_id          TEXT NOT NULL,
                    verifier_model  TEXT,
                    verdict         TEXT NOT NULL,
                    score           DOUBLE PRECISION,
                    critique        TEXT,
                    evidence_ref    TEXT,
                    staged_diff_sha TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_verification_runs ON verification_gate_runs(run_id)"
            )

            # ── P5 — Triage + Reflection + Memory ──
            # The base ``reflections`` and ``goals`` tables landed in P1.
            # P5 layers a handful of additive columns + indexes on top so a
            # fresh install never needs a multi-revision migration to receive
            # triage proposals or recall lessons. Every statement is idempotent
            # (``IF NOT EXISTS``) so re-running init_db() against a populated
            # DB is a no-op — matching the append-only contract called out in
            # the master plan §0.
            #
            # goals additions: triage writes proposals here with status
            # PENDING_APPROVAL; the dedup contract is the unique partial
            # index on (loop_id, source, source_external_id).
            conn.execute(
                "ALTER TABLE goals ADD COLUMN IF NOT EXISTS loop_id TEXT"
            )
            conn.execute(
                "ALTER TABLE goals ADD COLUMN IF NOT EXISTS title TEXT"
            )
            conn.execute(
                "ALTER TABLE goals ADD COLUMN IF NOT EXISTS status TEXT "
                "NOT NULL DEFAULT 'DRAFT'"
            )
            conn.execute(
                "ALTER TABLE goals ADD COLUMN IF NOT EXISTS source TEXT"
            )
            conn.execute(
                "ALTER TABLE goals ADD COLUMN IF NOT EXISTS source_external_id TEXT"
            )
            conn.execute(
                "ALTER TABLE goals ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_goals_status_loop_id "
                "ON goals (status, loop_id)"
            )
            # Triage dedup contract: at most one open proposal per
            # (loop_id, source, source_external_id) — the PARTIAL clause
            # lets manually-created goals (no source row) coexist.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_goals_loop_source "
                "ON goals (loop_id, source, source_external_id) "
                "WHERE source_external_id IS NOT NULL"
            )

            # reflections additions: the P1 base columns are scope-keyed.
            # P5 layers loop-shape columns (loop_run_id, outer_iteration)
            # so the loop runner can write rich rows while the legacy
            # generic shape stays usable for non-loop callers.
            conn.execute(
                "ALTER TABLE reflections ADD COLUMN IF NOT EXISTS loop_run_id TEXT"
            )
            conn.execute(
                "ALTER TABLE reflections ADD COLUMN IF NOT EXISTS outer_iteration INTEGER"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_reflections_loop_run "
                "ON reflections (loop_run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_reflections_scope_created "
                "ON reflections (scope_kind, scope_id, created_at DESC)"
            )

            # agent_memory — the P5 MemoryReadHandler / MemoryWriteHandler
            # stores per-loop lessons_index + last_digest_path pointers.
            # Schema is intentionally small (scope/key/value) so it stays
            # generic for non-loop callers; ``scope='loop:<loop_id>'`` is
            # the loop convention and enforced in app/loop/memory.py.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS loop_agent_memory (
                    scope      TEXT NOT NULL,
                    key        TEXT NOT NULL,
                    value      JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (scope, key)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_loop_agent_memory_scope ON loop_agent_memory(scope)"
            )

            conn.commit()
            logger.info('[AGENT] Loop Engineering tables ready (P1 schema)')
            logger.info('[AGENT] Loop Engineering P5 columns / indexes ready')
        logger.info('[AGENT] Workflow DB tables ready (using shared platform pool)')
        # ---- Studio governance tables --------------------------------------
        # Created here so the shared pool already exists. ``ensure_tables`` is
        # best-effort and no-ops if the governance package itself fails to
        # import (e.g. during partial deployments).
        if importlib.util.find_spec("app.governance") is not None:
            try:
                from app.governance import governance_store as _gov_store
                _gov_store.ensure_tables()
            except Exception:
                logger.exception('[AGENT] Studio governance tables init failed (non-fatal)')
            try:
                from app.governance import lifecycle as _gov_lifecycle
                _gov_lifecycle.ensure_status_columns()
            except Exception:
                logger.exception('[AGENT] Studio governance lifecycle column init failed (non-fatal)')

    try:
        await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f'[AGENT] Failed to initialise workflow DB: {e}')
 
 
async def close_db() -> None:
    """Detach from the shared pool on shutdown.

    ``_pool`` now points at the platform's single shared pool (owned by
    ``db.database.engine``), which must outlive ABStudio and be torn down by the
    platform, not here. So this only drops ABStudio's reference — it does NOT
    close the underlying connections. ``SHARED_POOL.close()`` is a no-op anyway.
    """
    global _pool
    _pool = None
 
 
# ---------------------------------------------------------------------------
# Workflow CRUD
# ---------------------------------------------------------------------------
 
async def get_all_workflows(owner_user_id: str) -> List[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            # ``knowledge`` is intentionally omitted from the dashboard list
            # SELECT — the workflow cards never render KB details, and pulling
            # the JSONB blob for every row would bloat the listing payload.
            # ``_row_to_workflow`` defaults missing columns to ``{mode:'none'}``.
            rows = conn.execute(
                "SELECT id, name, description, author, graph_data, created_at, updated_at, owner_user_id "
                "FROM workflows WHERE owner_user_id = %s ORDER BY updated_at DESC",
                (owner_user_id,),
            ).fetchall()
        return [_row_to_workflow(r) for r in rows]
    return await asyncio.to_thread(_run)
 
 
async def get_workflow(workflow_id: str, owner_user_id: str) -> Optional[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                "SELECT id, name, description, author, graph_data, created_at, updated_at, owner_user_id, knowledge, source_template_id "
                "FROM workflows WHERE id = %s AND owner_user_id = %s",
                (workflow_id, owner_user_id),
            ).fetchone()
        return _row_to_workflow(row) if row else None
    return await asyncio.to_thread(_run)


async def get_workflow_by_name(name: str, owner_user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a workflow by (name, owner) rather than by id.

    Governance identifies a submitted workflow by (name, owner_id) — there is no
    id in the inbox metadata. This is the read path the approver-only governance
    preview endpoint uses to render another user's submitted workflow. Scoped by
    owner_user_id so an approver only ever sees the specific submitter's graph.
    """
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                "SELECT id, name, description, author, graph_data, created_at, updated_at, owner_user_id, knowledge, source_template_id "
                "FROM workflows WHERE name = %s AND owner_user_id = %s",
                (name, owner_user_id),
            ).fetchone()
        return _row_to_workflow(row) if row else None
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Governance bridge — submit ABStudio artifacts for department-HOD approval.
# All calls are best-effort; a governance failure must never break create/save.
# ---------------------------------------------------------------------------

async def _governance(action, entity_type, name, owner_user_id,
                      *, content=None, description="", template_id=None):
    """Run one governance action (submit / reconcile / register-template) for an
    ABStudio artifact off the request thread. ``action`` selects the
    governance_client entry point; the creator's department is resolved from
    their id for HOD routing. All failures are swallowed to a debug log so a
    governance hiccup can never break artifact create/save."""
    if not name:
        return
    def _run():
        try:
            from app.core import governance_client as gc
            dept = gc.resolve_user_department(owner_user_id)
            common = dict(created_by=str(owner_user_id or ""), department=dept,
                          description=description or "")
            _hash = gc.canonical_hash(content) if content is not None else None
            if action == "reconcile":
                gc.reconcile_after_update(entity_type, name, content, **common,
                                          owner_id=str(owner_user_id or ""))
            elif action == "register_template":
                gc.mark_approved_template_instance(
                    entity_type, name, **common,
                    source_template_id=str(template_id) if template_id else None,
                    source_template_hash=_hash,
                )
            else:  # submit
                gc.submit_for_governance(entity_type, name, **common,
                                         source_template_hash=_hash)
        except Exception:
            logger.debug(f'[AGENT] governance {action} skipped for {entity_type}/{name}')
    await asyncio.to_thread(_run)


async def create_workflow(data: Dict[str, Any], owner_user_id: str, author: str,
                          governed: bool = False) -> Dict[str, Any]:
    _require_uri()
    import uuid
    wf_id = data.get("id") or f"workflow-{uuid.uuid4().hex[:12]}"
    now   = datetime.now(timezone.utc)
    name = _validate_entity_name_format(data.get("name", "New workflow"), "workflow")

    def _run():
        with _get_pool().connection() as conn:
            _ensure_unique_name(conn, "workflows", name, owner_user_id, exclude_id=wf_id, label="workflow")
            row = conn.execute(
                "INSERT INTO workflows "
                "(id, name, description, author, graph_data, created_at, updated_at, owner_user_id, knowledge, source_template_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, name, description, author, graph_data, "
                "created_at, updated_at, owner_user_id, knowledge, source_template_id",
                (
                    wf_id,
                    name,
                    data.get("description", ""),
                    author,
                    json.dumps(data.get("graphData", {"nodes": [], "edges": []})),
                    now, now,
                    owner_user_id,
                    json.dumps(data.get("knowledge", _KB_DEFAULT_BLOB)),
                    data.get("source_template_id") or None,
                ),
            ).fetchone()
        return _row_to_workflow(row)
    result = await asyncio.to_thread(_run)
    # NOTE: creating a workflow does NOT auto-submit it for approval. Submission
    # is an explicit user action (the "Submit for Approval" button) so opening
    # or autosaving a draft never spams approvers. A new workflow simply has no
    # governance record yet (ungoverned = the button is offered in the UI).
    return result
 
 
# ---------------------------------------------------------------------------
# Factory build sessions (agent / workflow / skill factory)
# ---------------------------------------------------------------------------


async def load_factory_session(
    session_id: str, factory_type: str, owner_user_id: str
) -> Optional[Dict[str, Any]]:
    """Return the persisted state blob for a factory build session, or None.

    Read-through counterpart to ``save_factory_session`` — lets an in-memory
    factory session be rehydrated after a backend restart. Scoped by
    ``owner_user_id`` so users can never load each other's build sessions.
    """
    _require_uri()

    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                "SELECT state FROM factory_sessions "
                "WHERE id = %s AND factory_type = %s AND owner_user_id = %s",
                (session_id, factory_type, owner_user_id),
            ).fetchone()
        return row[0] if row else None

    return await asyncio.to_thread(_run)


async def save_factory_session(
    session_id: str,
    factory_type: str,
    owner_user_id: str,
    state: Dict[str, Any],
) -> None:
    """Upsert a factory build session's full state blob.

    Idempotent on ``(id, owner_user_id)`` so repeated turns overwrite in
    place. Mirrors the cowork_conversations upsert pattern.
    """
    _require_uri()
    now = datetime.now(timezone.utc)

    def _run():
        with _get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO factory_sessions "
                "(id, factory_type, owner_user_id, state, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id, owner_user_id) DO UPDATE SET "
                "state = EXCLUDED.state, "
                "factory_type = EXCLUDED.factory_type, "
                "updated_at = EXCLUDED.updated_at",
                (session_id, factory_type, owner_user_id, json.dumps(state), now, now),
            )

    await asyncio.to_thread(_run)


async def delete_factory_session(
    session_id: str, factory_type: str, owner_user_id: str
) -> None:
    """Remove a persisted factory session (e.g. after a successful confirm)."""
    _require_uri()

    def _run():
        with _get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM factory_sessions "
                "WHERE id = %s AND factory_type = %s AND owner_user_id = %s",
                (session_id, factory_type, owner_user_id),
            )

    await asyncio.to_thread(_run)


class StaleWorkflowError(RuntimeError):
    """Raised when an update is rejected because the client's view is stale."""

    def __init__(self, current: Dict[str, Any]):
        super().__init__("workflow has been modified by another writer")
        self.current = current


class NameValidationError(ValueError):
    """Raised when a workflow or agent name fails validation."""


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _.\-&/,'():]{0,99}$")


def _validate_entity_name_format(name: str, entity_label: str) -> str:
    candidate = str(name or "").strip()
    label = entity_label.capitalize()
    if not candidate:
        raise NameValidationError(f"{label} name is required")
    if len(candidate) > 100:
        raise NameValidationError(f"{label} name must be 100 characters or fewer")
    if candidate.isdigit():
        raise NameValidationError(f"{label} name cannot be only numbers")
    if not candidate[0].isalpha():
        raise NameValidationError(f"{label} name must start with a letter")
    if not _NAME_RE.match(candidate):
        raise NameValidationError(
            f"{label} name can only contain letters, numbers, spaces, and _ . - & / , ' ( ) :"
        )
    return candidate


def _ensure_unique_name(conn, table: str, name: str, owner_user_id: str, *, exclude_id: str = "", label: str) -> None:
    row = conn.execute(
        f"SELECT id FROM {table} WHERE lower(name) = lower(%s) AND owner_user_id = %s AND id <> %s LIMIT 1",
        (name, owner_user_id, exclude_id or ""),
    ).fetchone()
    if row:
        raise NameValidationError(f"{label.capitalize()} name already exists")


def _generate_unique_name(table: str, base_name: str, owner_user_id: str) -> str:
    """Return a name derived from ``base_name`` that does not collide with an
    existing row for this owner. If ``base_name`` is free it is returned as-is;
    otherwise a numeric suffix (`" 2"`, `" 3"`, ...) is appended until a free
    name is found. Used by clone/instantiate flows (use-template, duplicate)
    so repeated use never raises a uniqueness 500.

    The suffix is kept within the 100-char name limit by trimming the base.
    """
    def _taken(conn, candidate: str) -> bool:
        return conn.execute(
            f"SELECT 1 FROM {table} WHERE lower(name) = lower(%s) AND owner_user_id = %s LIMIT 1",
            (candidate, owner_user_id),
        ).fetchone() is not None

    with _get_pool().connection() as conn:
        if not _taken(conn, base_name):
            return base_name
        for n in range(2, 1000):
            suffix = f" {n}"
            trimmed = base_name[: 100 - len(suffix)].rstrip()
            candidate = f"{trimmed}{suffix}"
            if not _taken(conn, candidate):
                return candidate
    # Extremely unlikely fallback: append a short random token.
    import uuid
    token = f" {uuid.uuid4().hex[:6]}"
    return f"{base_name[: 100 - len(token)].rstrip()}{token}"



async def update_workflow(
    workflow_id: str,
    data: Dict[str, Any],
    owner_user_id: str,
) -> Optional[Dict[str, Any]]:
    """Update a workflow.
 
    Accepts either ``graphData`` (preferred) or ``graph_data`` for the graph blob
    so the same shape returned by GET can be sent back without renaming.
 
    Optimistic concurrency: if ``data["expected_updated_at"]`` is provided and
    does not match the row's current ``updated_at``, raises StaleWorkflowError
    carrying the current row. Callers can surface this as HTTP 409 so the
    frontend can merge instead of silently overwriting a concurrent save.
    """
    _require_uri()
    now = datetime.now(timezone.utc)
    expected_updated_at = data.get("expected_updated_at")
    name = _validate_entity_name_format(data["name"], "workflow") if "name" in data else None

    graph_data = data.get("graphData", data.get("graph_data"))
    has_graph_data = "graphData" in data or "graph_data" in data
 
    # Guard: refuse to overwrite a non-empty workflow with an empty graph.
    # This protects against the frontend autosaving before its initial fetch
    # completes (which would otherwise clobber the user's work with `{nodes: [], edges: []}`).
    allow_empty = bool(data.get("allow_empty_graph"))
    incoming_empty = (
        has_graph_data
        and isinstance(graph_data, dict)
        and not (graph_data.get("nodes") or graph_data.get("edges"))
    )
 
    if has_graph_data and _LOG_LEVEL <= logging.DEBUG:
        try:
            nodes = (graph_data or {}).get("nodes") or []
            tool_counts = {
                n.get("id", "?"): len(((n.get("data") or {}).get("tools") or []))
                for n in nodes if n.get("type") == "agent"
            }
            logger.debug(f'[AGENT] update_workflow {workflow_id}: {len(nodes)} nodes, agent_tool_counts={tool_counts}')
        except Exception:
            pass
 
    def _run():
        nonlocal has_graph_data
        with _get_pool().connection() as conn:
            with conn.transaction():
                if expected_updated_at or (incoming_empty and not allow_empty):
                    current = conn.execute(
                        "SELECT graph_data, updated_at FROM workflows "
                        "WHERE id = %s AND owner_user_id = %s FOR UPDATE",
                        (workflow_id, owner_user_id),
                    ).fetchone()
                    if current is None:
                        return None
                    current_graph, current_updated = current
                    current_iso = current_updated.isoformat() if current_updated else None
                    if expected_updated_at and current_iso != expected_updated_at:
                        full = conn.execute(
                            "SELECT id, name, description, author, graph_data, "
                            "created_at, updated_at, owner_user_id, knowledge "
                            "FROM workflows WHERE id = %s AND owner_user_id = %s",
                            (workflow_id, owner_user_id),
                        ).fetchone()
                        raise StaleWorkflowError(_row_to_workflow(full))
                    if incoming_empty and not allow_empty:
                        stored_nodes = (current_graph or {}).get("nodes") or []
                        stored_edges = (current_graph or {}).get("edges") or []
                        if stored_nodes or stored_edges:
                            logger.warning(f'[AGENT] update_workflow {workflow_id}: refusing to overwrite non-empty graph with empty payload (set allow_empty_graph=true to force)')
                            has_graph_data = False  # skip graph_data update; keep other fields
 
                fields, values = [], []
                if name is not None:
                    _ensure_unique_name(conn, "workflows", name, owner_user_id, exclude_id=workflow_id, label="workflow")
                    fields.append("name = %s"); values.append(name)
                if "description" in data:
                    fields.append("description = %s"); values.append(data["description"])
                if has_graph_data:
                    fields.append("graph_data = %s"); values.append(json.dumps(graph_data))
                if "knowledge" in data:
                    # Workflow-level KB attachment. Accepts the same blob
                    # shape used by ``agents.knowledge`` / the per-node
                    # ``data.knowledge`` field. The engine falls back to
                    # this when a node's blob has ``mode == 'none'``.
                    fields.append("knowledge = %s"); values.append(json.dumps(data["knowledge"]))
                fields.append("updated_at = %s"); values.append(now)
                values.extend([workflow_id, owner_user_id])
                row = conn.execute(
                    f"UPDATE workflows SET {', '.join(fields)} "
                    f"WHERE id = %s AND owner_user_id = %s "
                    f"RETURNING id, name, description, author, graph_data, "
                    f"created_at, updated_at, owner_user_id, knowledge",
                    values,
                ).fetchone()
            return _row_to_workflow(row) if row else None
    result = await asyncio.to_thread(_run)
    # An edit that changes the graph flips the workflow back to PENDING_APPROVAL
    # (this is also how a modified template instance loses its pre-approval).
    if result:
        await _governance(
            "reconcile", "workflows", result.get("name"), owner_user_id,
            content=result.get("graphData", {}), description=result.get("description", ""),
        )
    return result


async def delete_workflow(workflow_id: str, owner_user_id: str) -> bool:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            conn.execute("DELETE FROM workflows WHERE id = %s AND owner_user_id = %s", (workflow_id, owner_user_id))
            conn.commit()
        return True
    return await asyncio.to_thread(_run)
 
 
async def duplicate_workflow(workflow_id: str, owner_user_id: str, author: str) -> Optional[Dict[str, Any]]:
    import uuid
    original = await get_workflow(workflow_id, owner_user_id)
    if not original:
        return None
    return await create_workflow({
        **original,
        "id":   f"workflow-{uuid.uuid4().hex[:12]}",
        "name": f"{original['name']} (Copy)",
    }, owner_user_id, author)
 
 
# ---------------------------------------------------------------------------
# Template operations
# ---------------------------------------------------------------------------
 
async def get_all_templates(department: str = "", is_admin: bool = False) -> List[Dict[str, Any]]:
    """List catalog templates visible to the caller.

    Visibility scoping (mirrors routers/agents_router.py agents_catalog):
    admins see everything; everyone else sees public templates plus their own
    department's private templates. An empty ``department`` sees only public.
    """
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT id, name, description, category, graph_data, pattern, hitl, "
                "visibility, department "
                "FROM templates "
                "WHERE NOT (id = ANY(%s)) "
                "  AND (%s OR visibility = 'public' "
                "       OR (visibility = 'private' AND department = %s)) "
                "ORDER BY name",
                (list(HIDDEN_TEMPLATE_IDS), bool(is_admin), department or None),
            ).fetchall()
        return [_row_to_template(r) for r in rows]
    return await asyncio.to_thread(_run)


async def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    # Hidden (web-search) templates are not addressable through the
    # public read API even by direct id, so a stale link to one returns
    # a clean 404 instead of leaking the row.
    if template_id in HIDDEN_TEMPLATE_IDS:
        return None
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                "SELECT id, name, description, category, graph_data, pattern, hitl, "
                "visibility, department "
                "FROM templates WHERE id = %s",
                (template_id,),
            ).fetchone()
        return _row_to_template(row) if row else None
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Publish-on-approval — copy an approved workflow/agent into the shared catalog.
# Called from the platform governance approve hook after a "Deploy" request is
# approved. Keyed by artifact NAME (governance's key). A repeat publish of the
# same name updates the existing catalog row rather than duplicating. Idempotency
# is best-effort: two truly concurrent approvals of the same name could each miss
# the existing-row check and create two rows — not a concern given publish is a
# single HOD action per artifact. Best-effort — callers swallow failures so
# approval never breaks.
# ---------------------------------------------------------------------------

# Catalog visibility domain (mirrors api/governance.py::_VALID_VISIBILITY). Note
# the fallback is the permissive value 'public'; callers validate before calling.
_TEMPLATE_VISIBILITIES = ("public", "private")


def _normalize_visibility(visibility: Optional[str]) -> str:
    return visibility if visibility in _TEMPLATE_VISIBILITIES else "public"


async def publish_workflow_as_template(
    name: str, *, visibility: str = "public", department: Optional[str] = None,
) -> Optional[str]:
    """Publish the workflow named ``name`` into the ``templates`` catalog.

    Returns the template id, or None if no such workflow exists. Visibility is
    'public' (all users) or 'private' (``department`` only). ``category`` is set
    to the department (or 'general') so the catalog's category facet groups
    published artifacts by owning team.
    """
    _require_uri()
    vis = _normalize_visibility(visibility)
    def _run():
        with _get_pool().connection() as conn:
            wf = conn.execute(
                "SELECT description, graph_data FROM workflows WHERE name = %s "
                "ORDER BY updated_at DESC LIMIT 1",
                (name,),
            ).fetchone()
            if not wf:
                return None
            # Reuse an existing catalog row for the same name (avoid dupes on
            # re-approval); otherwise create a fresh template id.
            existing = conn.execute(
                "SELECT id FROM templates WHERE name = %s LIMIT 1", (name,),
            ).fetchone()
            tid = existing[0] if existing else f"template-{uuid.uuid4().hex[:12]}"
            category = department or "general"
            conn.execute(
                "INSERT INTO templates "
                "(id, name, description, category, graph_data, visibility, department) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  description = EXCLUDED.description, graph_data = EXCLUDED.graph_data, "
                "  visibility = EXCLUDED.visibility, department = EXCLUDED.department",
                (tid, name, wf[0] or "", category,
                 json.dumps(wf[1]), vis, department),
            )
            conn.commit()
            return tid
    return await asyncio.to_thread(_run)


async def publish_agent_as_template(
    name: str, *, visibility: str = "public", department: Optional[str] = None,
) -> Optional[str]:
    """Publish the agent named ``name`` into the ``agent_templates`` catalog.

    Returns the agent-template id, or None if no such agent exists.
    """
    _require_uri()
    vis = _normalize_visibility(visibility)
    def _run():
        with _get_pool().connection() as conn:
            ag = conn.execute(
                "SELECT name, description, instructions, provider, model_name, "
                "temperature, max_tokens, top_p, tools, skills, knowledge, "
                "use_subagents, id "
                "FROM agents WHERE name = %s ORDER BY updated_at DESC LIMIT 1",
                (name,),
            ).fetchone()
            if not ag:
                return None
            existing = conn.execute(
                "SELECT id FROM agent_templates WHERE name = %s LIMIT 1", (name,),
            ).fetchone()
            tid = existing[0] if existing else f"agent-preset-{uuid.uuid4().hex[:12]}"
            category = department or "general"
            conn.execute(
                "INSERT INTO agent_templates "
                "(id, name, description, category, instructions, provider, model_name, "
                " temperature, max_tokens, top_p, tools, skills, visibility, department, "
                " knowledge, use_subagents, source_agent_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  description = EXCLUDED.description, instructions = EXCLUDED.instructions, "
                "  model_name = EXCLUDED.model_name, tools = EXCLUDED.tools, "
                "  skills = EXCLUDED.skills, visibility = EXCLUDED.visibility, "
                "  department = EXCLUDED.department, knowledge = EXCLUDED.knowledge, "
                "  use_subagents = EXCLUDED.use_subagents, "
                "  source_agent_id = EXCLUDED.source_agent_id",
                (tid, ag[0], ag[1] or "", category, ag[2] or "", ag[3] or "custom",
                 ag[4] or "", ag[5] if ag[5] is not None else 0.7,
                 ag[6] if ag[6] is not None else 8192, ag[7] if ag[7] is not None else 1.0,
                 json.dumps(ag[8] if ag[8] is not None else []),
                 json.dumps(ag[9] if ag[9] is not None else []), vis, department,
                 json.dumps(ag[10] if ag[10] is not None else {"mode": "none"}),
                 bool(ag[11]) if ag[11] is not None else False,
                 ag[12]),
            )
            conn.commit()
            return tid
    return await asyncio.to_thread(_run)


async def use_template(template_id: str, owner_user_id: str, author: str) -> Optional[Dict[str, Any]]:
    """Create a new workflow from a template.

    Idempotent per (owner, template): if this user already has a workflow
    cloned from the template, return the existing row instead of creating
    another duplicate in "My Workflows".
    """
    template = await get_template(template_id)
    if not template:
        return None

    def _find_existing():
        with _get_pool().connection() as conn:
            row = conn.execute(
                "SELECT id, name, description, author, graph_data, created_at, updated_at, owner_user_id, knowledge, source_template_id "
                "FROM workflows WHERE owner_user_id = %s AND source_template_id = %s",
                (owner_user_id, template_id),
            ).fetchone()
        return row

    existing_row = await asyncio.to_thread(_find_existing)
    if existing_row:
        return _row_to_workflow(existing_row)

    import copy, uuid
    graph = copy.deepcopy(template["graphData"])
    unique_name = await asyncio.to_thread(
        _generate_unique_name, "workflows", template["name"], owner_user_id
    )
    result = await create_workflow({
        "id":                 f"workflow-{uuid.uuid4().hex[:12]}",
        "name":               unique_name,
        "description":        f"Created from template: {template['name']}",
        "graphData":          graph,
        "source_template_id": template_id,
    }, owner_user_id, author, governed=True)
    if result:
        await _governance(
            "register_template", "workflows", result.get("name"), owner_user_id,
            template_id=template_id, content=graph,
            description=result.get("description", ""),
        )
    return result


# ---------------------------------------------------------------------------
# Optional template editor support
# ---------------------------------------------------------------------------
# These helpers exist solely so the feature-flagged `template_admin` router
# can mutate templates from the UI. They are NOT used by the read API, the
# seed loop, `use_template`, or any other path. If the editor feature is
# removed, this whole block can be deleted alongside `app/api/template_admin.py`
# with zero impact on the rest of the file.

# Columns the editor is allowed to touch. `id` is immutable (it's the
# stable handle every workflow clone and trigger references).
_EDITABLE_TEMPLATE_FIELDS: Dict[str, str] = {
    "name":        "name",
    "description": "description",
    "category":    "category",
    "pattern":     "pattern",
    "hitl":        "hitl",
    "graphData":   "graph_data",   # the UI sends camelCase; DB uses snake_case
    "graph_data":  "graph_data",
}


async def update_template(
    template_id: str,
    data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Persist edits to a template row. Hidden templates and unknown IDs
    return None so the router can 404 cleanly. `id` cannot be changed."""
    if template_id in HIDDEN_TEMPLATE_IDS:
        return None
    _require_uri()

    # Map incoming fields → DB columns and drop anything outside the
    # whitelist so a malformed payload can't corrupt unrelated columns.
    updates: Dict[str, Any] = {}
    for incoming, value in data.items():
        col = _EDITABLE_TEMPLATE_FIELDS.get(incoming)
        if col is None:
            continue
        if col == "graph_data":
            updates[col] = json.dumps(value)
        elif col == "hitl":
            updates[col] = bool(value)
        else:
            updates[col] = value

    if not updates:
        # Nothing valid in the payload — return the current row unchanged
        # so the UI can re-render without a 400.
        return await get_template(template_id)

    set_clause = ", ".join(f"{col} = %s" for col in updates)
    params = list(updates.values()) + [template_id]

    def _run():
        with _get_pool().connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    f"UPDATE templates SET {set_clause} WHERE id = %s "
                    "RETURNING id, name, description, category, graph_data, pattern, hitl",
                    params,
                ).fetchone()
        return _row_to_template(row) if row else None
    return await asyncio.to_thread(_run)


async def delete_template(template_id: str) -> bool:
    """Delete a template row. Returns True if a row was removed.

    The seed loop will NOT re-insert it on restart because the incremental
    seed only inserts brand-new IDs — so a deleted template stays deleted
    until either the table is wiped or `reset_template_to_seed` is called."""
    if template_id in HIDDEN_TEMPLATE_IDS:
        return False
    _require_uri()

    def _run():
        with _get_pool().connection() as conn:
            with conn.transaction():
                affected = conn.execute(
                    "DELETE FROM templates WHERE id = %s", (template_id,),
                ).rowcount or 0
        return affected > 0
    return await asyncio.to_thread(_run)


async def reset_template_to_seed(template_id: str) -> Optional[Dict[str, Any]]:
    """Restore a template to its `_SEED_TEMPLATES` definition. Works for
    rows that were edited AND for rows that were deleted (re-inserts in
    that case). Returns the restored row, or None if the ID isn't a
    known seed."""
    if template_id in HIDDEN_TEMPLATE_IDS:
        return None
    seed = next((t for t in _SEED_TEMPLATES if t["id"] == template_id), None)
    if seed is None:
        return None
    _require_uri()

    payload = (
        seed["id"], seed["name"], seed["description"], seed["category"],
        json.dumps(seed["graph_data"]),
        seed.get("pattern", "sequential"),
        bool(seed.get("hitl", False)),
    )

    def _run():
        with _get_pool().connection() as conn:
            with conn.transaction():
                # UPSERT: a single statement covers both the "edited row"
                # case (UPDATE) and the "deleted row" case (INSERT).
                row = conn.execute(
                    "INSERT INTO templates "
                    "(id, name, description, category, graph_data, pattern, hitl) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "name = EXCLUDED.name, description = EXCLUDED.description, "
                    "category = EXCLUDED.category, graph_data = EXCLUDED.graph_data, "
                    "pattern = EXCLUDED.pattern, hitl = EXCLUDED.hitl "
                    "RETURNING id, name, description, category, graph_data, pattern, hitl",
                    payload,
                ).fetchone()
        return _row_to_template(row) if row else None
    return await asyncio.to_thread(_run)


async def save_template_to_seed(template_id: str) -> Optional[Dict[str, Any]]:
    """Patch the template's literal entry inside `_SEED_TEMPLATES` in
    this module's source file so the DB state becomes the new code
    baseline. Future "Reset to seed" calls will restore THIS saved
    version instead of the original literal.

    Returns the saved template dict, or None if the row is unknown /
    hidden."""
    if template_id in HIDDEN_TEMPLATE_IDS:
        return None
    _require_uri()

    def _read():
        with _get_pool().connection() as conn:
            row = conn.execute(
                "SELECT id, name, description, category, graph_data, pattern, hitl "
                "FROM templates WHERE id = %s",
                (template_id,),
            ).fetchone()
        return row

    row = await asyncio.to_thread(_read)
    if not row:
        return None

    entry: Dict[str, Any] = {
        "id":          row[0],
        "name":        row[1],
        "description": row[2],
        "category":    row[3],
        "graph_data":  row[4] if row[4] is not None else {},
        "pattern":     row[5] if row[5] is not None else "sequential",
        "hitl":        bool(row[6]) if row[6] is not None else False,
    }

    await asyncio.to_thread(_upsert_seed_entry, entry)
    return _row_to_template(row)


async def create_template(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Insert a brand-new template row AND append it to `_SEED_TEMPLATES`
    in this module's source so it survives a DB wipe.

    Required fields: `id`, `name`, `graphData` (or `graph_data`).
    Optional: `description`, `category`, `pattern`, `hitl`."""
    template_id = (data.get("id") or "").strip()
    name        = (data.get("name") or "").strip()
    graph       = data.get("graphData") or data.get("graph_data")
    if not template_id or not name or not isinstance(graph, dict):
        return None
    if template_id in HIDDEN_TEMPLATE_IDS:
        return None
    _require_uri()

    description = (data.get("description") or "").strip()
    category    = (data.get("category") or "general").strip() or "general"
    pattern     = (data.get("pattern") or "sequential").strip() or "sequential"
    hitl        = bool(data.get("hitl", False))

    payload = (
        template_id, name, description, category,
        json.dumps(graph), pattern, hitl,
    )

    def _run():
        with _get_pool().connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "INSERT INTO templates "
                    "(id, name, description, category, graph_data, pattern, hitl) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING "
                    "RETURNING id, name, description, category, graph_data, pattern, hitl",
                    payload,
                ).fetchone()
        return _row_to_template(row) if row else None

    created = await asyncio.to_thread(_run)
    if not created:
        return None

    entry: Dict[str, Any] = {
        "id":          template_id,
        "name":        name,
        "description": description,
        "category":    category,
        "graph_data":  graph,
        "pattern":     pattern,
        "hitl":        hitl,
    }
    await asyncio.to_thread(_upsert_seed_entry, entry)
    return created


async def export_templates_snapshot() -> List[Dict[str, Any]]:
    """Dump the current `templates` table as a list of plain dicts so a
    developer can hand-merge edits back into `_SEED_TEMPLATES`. Hidden
    templates are NOT included — they're considered authoritative in code."""
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT id, name, description, category, graph_data, pattern, hitl "
                "FROM templates WHERE NOT (id = ANY(%s)) ORDER BY id",
                (list(HIDDEN_TEMPLATE_IDS),),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "id":          r[0],
                "name":        r[1],
                "description": r[2],
                "category":    r[3],
                "graph_data":  r[4],
                "pattern":     r[5] if r[5] is not None else "sequential",
                "hitl":        bool(r[6]) if r[6] is not None else False,
            })
        return out
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Agent Templates
# ---------------------------------------------------------------------------
 
_AGENT_TEMPLATE_COLS = (
    "id, name, description, category, instructions, provider, model_name, "
    "temperature, max_tokens, top_p, tools, skills, visibility, department, "
    "knowledge, use_subagents, source_agent_id"
)
 
 
def _row_to_agent_template(row) -> Dict[str, Any]:
    return {
        "id":           row[0],
        "name":         row[1],
        "description":  row[2],
        "category":     row[3],
        "instructions": row[4],
        "provider":     row[5],
        "model_name":   row[6],
        "temperature":  row[7],
        "max_tokens":   row[8],
        "top_p":        row[9],
        "tools":        row[10] if row[10] is not None else [],
        "skills":       row[11] if row[11] is not None else [],
        "visibility":   row[12] if len(row) > 12 and row[12] is not None else "public",
        "department":   row[13] if len(row) > 13 else None,
        "knowledge":    row[14] if len(row) > 14 and row[14] is not None else {"mode": "none"},
        "use_subagents": bool(row[15]) if len(row) > 15 and row[15] is not None else False,
        "source_agent_id": row[16] if len(row) > 16 else None,
    }


async def get_all_agent_templates(department: str = "", is_admin: bool = False) -> List[Dict[str, Any]]:
    """List agent presets visible to the caller.

    Same department scoping as get_all_templates: admins see all; others see
    public presets plus their own department's private presets.
    """
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            rows = conn.execute(
                f"SELECT {_AGENT_TEMPLATE_COLS} FROM agent_templates "
                "WHERE (%s OR visibility = 'public' "
                "       OR (visibility = 'private' AND department = %s)) "
                "ORDER BY name",
                (bool(is_admin), department or None),
            ).fetchall()
        return [_row_to_agent_template(r) for r in rows]
    return await asyncio.to_thread(_run)
 
 
async def get_agent_template(
    template_id: str,
    department: str = "",
    is_admin: bool = False,
) -> Optional[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT {_AGENT_TEMPLATE_COLS} FROM agent_templates WHERE id = %s "
                "AND (%s OR visibility = 'public' "
                "     OR (visibility = 'private' AND department = %s))",
                (template_id, bool(is_admin), department or None),
            ).fetchone()
        return _row_to_agent_template(row) if row else None
    return await asyncio.to_thread(_run)


async def use_agent_template(
    template_id: str,
    owner_user_id: str,
    department: str = "",
    is_admin: bool = False,
) -> Optional[Dict[str, Any]]:
    """Create a new agent by cloning an agent template.

    Idempotent: if the user already has an agent created from this template,
    return the existing agent instead of creating another duplicate.
    """
    template = await get_agent_template(template_id, department=department, is_admin=is_admin)
    if not template:
        return None

    existing = await get_agent_by_source_template(template_id, owner_user_id)
    if existing:
        return existing

    import copy
    unique_name = await asyncio.to_thread(
        _generate_unique_name, "agents", template["name"], owner_user_id
    )
    payload = {
        "name":               unique_name,
        "description":        f"Created from template: {template['name']}",
        "instructions":       template["instructions"],
        "provider":           template["provider"],
        "model_name":         template["model_name"],
        "temperature":        template["temperature"],
        "max_tokens":         template["max_tokens"],
        "top_p":              template["top_p"],
        "tools":              copy.deepcopy(template["tools"]),
        "skills":             copy.deepcopy(template["skills"]),
        # Carry the template's KB attachment through to the cloned agent.
        # Previously omitted, so a template built with an "existing_kb"
        # selection always cloned into an agent with {"mode": "none"} — the
        # reported "knowledge moves to None" bug. Fall back to the default
        # blob for older templates that predate the agent_templates.knowledge
        # column.
        "knowledge":          copy.deepcopy(template.get("knowledge") or {"mode": "none"}),
        # Carry the subagents/swarm toggle through to the cloned agent.
        # Previously omitted, so the clone always reset to False.
        "use_subagents":      bool(template.get("use_subagents", False)),
        "source_template_id": template_id,
    }
    result = await create_agent(payload, owner_user_id, governed=True)
    if result:
        # Copy the source agent's triggers/routines onto the cloned agent.
        # Triggers are keyed by (target_kind, target_id) where target_id is the
        # agent id; the clone gets a brand-new id, so without this step the
        # deployed/template-cloned agent shows no triggers. Best-effort: a
        # trigger-copy failure must not fail the whole "use template" action.
        try:
            await _copy_agent_triggers(
                template.get("source_agent_id"), result.get("id"), owner_user_id,
            )
        except Exception:
            logger.exception(
                "[AGENT] use_agent_template: trigger copy failed for template %s",
                template_id,
            )
        _content = {k: result.get(k) for k in
                    ("instructions", "model_name", "tools", "skills",
                     "guardrails", "memory_config", "attached_flows")}
        await _governance(
            "register_template", "agents", result.get("name"), owner_user_id,
            template_id=template_id, content=_content,
            description=result.get("description", ""),
        )
    return result


async def _copy_agent_triggers(
    src_agent_id: Optional[str],
    dst_agent_id: Optional[str],
    owner_user_id: str,
) -> int:
    """Clone every trigger from ``src_agent_id`` onto ``dst_agent_id``.

    Triggers are hard-bound to an agent id via ``triggers.target_id``. When an
    agent is duplicated or a template is instantiated the new agent gets a
    fresh id, so its routines have to be re-created pointing at that id. Each
    copy is registered with the scheduler if enabled, exactly like a
    user-created trigger. Returns the number of triggers copied.

    No-op (returns 0) when either id is missing — e.g. a seeded template that
    has no originating agent to copy from.
    """
    if not src_agent_id or not dst_agent_id:
        return 0
    src_triggers = await list_triggers(owner_user_id, "agent", src_agent_id)
    copied = 0
    for t in src_triggers:
        payload = {
            "target_kind": "agent",
            "target_id":   dst_agent_id,
            "node_id":     t.get("node_id") or None,
            "name":        t.get("name", "") or "",
            "schedule":    t.get("schedule") or {},
            "input_text":  t.get("input_text", "") or "",
            "enabled":     bool(t.get("enabled", True)),
        }
        new_trigger = await create_trigger(payload, owner_user_id)
        copied += 1
        if new_trigger.get("enabled"):
            try:
                from app.services import trigger_scheduler
                next_run = trigger_scheduler.register_trigger(new_trigger)
                if next_run is not None:
                    await update_trigger_run_metadata(
                        new_trigger["id"], next_run_at=next_run,
                    )
            except Exception:
                logger.exception(
                    "[AGENT] _copy_agent_triggers: scheduler register failed for %s",
                    new_trigger.get("id"),
                )
    if copied:
        logger.info(
            "[AGENT] _copy_agent_triggers: copied %d trigger(s) %s -> %s",
            copied, src_agent_id, dst_agent_id,
        )
    return copied
 
 
# ---------------------------------------------------------------------------
# Agents CRUD
# ---------------------------------------------------------------------------
 
def _row_to_agent(row) -> Dict[str, Any]:
    return {
        "id":                 row[0],
        "name":               row[1],
        "description":        row[2],
        "instructions":       row[3],
        "provider":           row[4],
        "model_name":         row[5],
        "api_key":            row[6],
        "temperature":        row[7],
        "max_tokens":         row[8],
        "top_p":              row[9],
        "base_url":           row[10],
        "created_at":         row[11].isoformat() if row[11] else None,
        "updated_at":         row[12].isoformat() if row[12] else None,
        "owner_user_id":      row[13],
        "tools":              row[14] if row[14] is not None else [],
        "skills":             row[15] if row[15] is not None else [],
        "guardrails":         row[16] if row[16] is not None else {},
        "memory_config":      row[17] if row[17] is not None else {},
        "knowledge":          row[18] if row[18] is not None else {"mode": "none"},
        "attached_flows":     row[19] if row[19] is not None else [],
        "use_subagents":      bool(row[20]) if row[20] is not None else False,
        "source_template_id": row[21] if len(row) > 21 else None,
        # Optional per-agent sample document (look-and-feel reference).
        # Empty dict when unset; shape is documented at the DDL site.
        "sample_doc":         row[22] if len(row) > 22 and row[22] is not None else {},
    }


_AGENT_SELECT = (
    "SELECT id, name, description, instructions, provider, model_name, api_key, "
    "temperature, max_tokens, top_p, base_url, created_at, updated_at, owner_user_id, "
    "tools, skills, guardrails, memory_config, knowledge, attached_flows, use_subagents, "
    "source_template_id, sample_doc "
    "FROM agents"
)
 
 
async def get_all_agents(owner_user_id: str) -> List[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            rows = conn.execute(
                f"{_AGENT_SELECT} WHERE owner_user_id = %s ORDER BY updated_at DESC",
                (owner_user_id,),
            ).fetchall()
        return [_row_to_agent(r) for r in rows]
    return await asyncio.to_thread(_run)
 
 
async def get_agent(agent_id: str, owner_user_id: str) -> Optional[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                f"{_AGENT_SELECT} WHERE id = %s AND owner_user_id = %s",
                (agent_id, owner_user_id),
            ).fetchone()
        return _row_to_agent(row) if row else None
    return await asyncio.to_thread(_run)
 
 
async def get_agent_by_name(name: str, owner_user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch an agent by (name, owner) rather than by id.

    Governance identifies a submitted agent by (name, owner_id) — there is no id
    in the inbox metadata. This is the read path the approver-only governance
    preview endpoint uses to render another user's submitted agent's full config
    (instructions, tools, skills). Scoped by owner_user_id so an approver only
    ever sees the specific submitter's agent.
    """
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                f"{_AGENT_SELECT} WHERE name = %s AND owner_user_id = %s",
                (name, owner_user_id),
            ).fetchone()
        return _row_to_agent(row) if row else None
    return await asyncio.to_thread(_run)


async def get_agent_by_source_template(template_id: str, owner_user_id: str) -> Optional[Dict[str, Any]]:
    """Return the agent created from a given template by this user, if any."""
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                f"{_AGENT_SELECT} WHERE source_template_id = %s AND owner_user_id = %s",
                (template_id, owner_user_id),
            ).fetchone()
        return _row_to_agent(row) if row else None
    return await asyncio.to_thread(_run)


async def get_agent_by_id(agent_id: str) -> Optional[Dict[str, Any]]:
    """
    Look up an agent by id with no owner filter. Used by AgentRunner at
    runtime where we don't have user context (the chat endpoint uses the
    auth stub's local-dev-user, but production deployments may want
    multi-user lookups without going through the owner check).
    """
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                f"{_AGENT_SELECT} WHERE id = %s",
                (agent_id,),
            ).fetchone()
        return _row_to_agent(row) if row else None
    return await asyncio.to_thread(_run)


async def create_agent(data: Dict[str, Any], owner_user_id: str,
                       governed: bool = False) -> Dict[str, Any]:
    _require_uri()
    import uuid
    agent_id = data.get("id") or f"agent-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    name = _validate_entity_name_format(data.get("name", "New Agent"), "agent")

    def _run():
        with _get_pool().connection() as conn:
            _ensure_unique_name(conn, "agents", name, owner_user_id, exclude_id=agent_id, label="agent")
            row = conn.execute(
                "INSERT INTO agents "
                "(id, name, description, instructions, provider, model_name, api_key, "
                "temperature, max_tokens, top_p, base_url, created_at, updated_at, owner_user_id, "
                "tools, skills, guardrails, memory_config, knowledge, attached_flows, use_subagents, "
                "source_template_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, name, description, instructions, provider, model_name, api_key, "
                "temperature, max_tokens, top_p, base_url, created_at, updated_at, owner_user_id, "
                "tools, skills, guardrails, memory_config, knowledge, attached_flows, use_subagents, "
                "source_template_id, sample_doc",
                (
                    agent_id,
                    name,
                    data.get("description", ""),
                    data.get("instructions", ""),
                    "custom",
                    data.get("model_name", ""),
                    data.get("api_key", ""),
                    float(data.get("temperature", 0.7)),
                    int(data.get("max_tokens", 8192)),
                    float(data.get("top_p", 1.0)),
                    data.get("base_url", ""),
                    now, now,
                    owner_user_id,
                    json.dumps(data.get("tools", [])),
                    json.dumps(data.get("skills", [])),
                    json.dumps(data.get("guardrails", {})),
                    json.dumps(data.get("memory_config", {})),
                    json.dumps(data.get("knowledge", {"mode": "none"})),
                    json.dumps(data.get("attached_flows", [])),
                    bool(data.get("use_subagents", False)),
                    data.get("source_template_id") or None,
                ),
            ).fetchone()
        return _row_to_agent(row)
    result = await asyncio.to_thread(_run)
    # NOTE: creating an agent does NOT auto-submit it for approval — submission
    # is an explicit user action (see create_workflow for rationale).
    return result


async def update_agent(agent_id: str, data: Dict[str, Any], owner_user_id: str) -> Optional[Dict[str, Any]]:
    _require_uri()
    now = datetime.now(timezone.utc)
    name = _validate_entity_name_format(data["name"], "agent") if "name" in data else None

    def _run():
        with _get_pool().connection() as conn:
            with conn.transaction():
                fields, values = [], []
                if name is not None:
                    _ensure_unique_name(conn, "agents", name, owner_user_id, exclude_id=agent_id, label="agent")
                    fields.append("name = %s")
                    values.append(name)
                for col in ("description", "instructions", "provider",
                            "model_name", "api_key", "temperature", "max_tokens", "top_p", "base_url"):
                    if col in data:
                        fields.append(f"{col} = %s")
                        values.append(data[col])
                if "use_subagents" in data:
                    fields.append("use_subagents = %s")
                    values.append(bool(data["use_subagents"]))
                for col in ("tools", "skills", "guardrails", "memory_config", "knowledge", "attached_flows"):
                    if col in data:
                        fields.append(f"{col} = %s")
                        values.append(json.dumps(data[col]))
                fields.append("updated_at = %s")
                values.append(now)
                values.extend([agent_id, owner_user_id])
                row = conn.execute(
                    f"UPDATE agents SET {', '.join(fields)} "
                    f"WHERE id = %s AND owner_user_id = %s "
                    f"RETURNING id, name, description, instructions, provider, model_name, api_key, "
                    f"temperature, max_tokens, top_p, base_url, created_at, updated_at, owner_user_id, "
                    f"tools, skills, guardrails, memory_config, knowledge, attached_flows, use_subagents, "
                    f"source_template_id, sample_doc",
                    values,
                ).fetchone()
        return _row_to_agent(row) if row else None
    result = await asyncio.to_thread(_run)
    if result:
        _content = {k: result.get(k) for k in
                    ("instructions", "model_name", "tools", "skills",
                     "guardrails", "memory_config", "attached_flows")}
        await _governance(
            "reconcile", "agents", result.get("name"), owner_user_id,
            content=_content, description=result.get("description", ""),
        )
    return result


async def delete_agent(agent_id: str, owner_user_id: str) -> bool:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM agents WHERE id = %s AND owner_user_id = %s",
                (agent_id, owner_user_id),
            )
            conn.commit()
            return (cur.rowcount or 0) > 0
    return await asyncio.to_thread(_run)


async def set_agent_sample_doc(
    agent_id: str,
    owner_user_id: str,
    sample_doc: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Persist the ``sample_doc`` metadata blob on an agent row.

    The physical file itself is written to disk by the upload endpoint
    (see ``app/api/agent_sample.py``); this function only records the
    resulting metadata (path, kind, name, size, notes, uploaded_at) so
    the runtime can find the file when the agent runs. Ownership is
    enforced — an agent belonging to a different user cannot be
    modified through this path.

    Returns the updated agent row (via ``_row_to_agent``) or ``None`` if
    no row was updated (wrong id / wrong owner).
    """
    _require_uri()
    now = datetime.now(timezone.utc)

    def _run():
        with _get_pool().connection() as conn:
            # RETURNING must list columns in the same order _row_to_agent
            # positionally maps — keep in sync with _AGENT_SELECT.
            row = conn.execute(
                "UPDATE agents SET sample_doc = %s, updated_at = %s "
                "WHERE id = %s AND owner_user_id = %s "
                "RETURNING id, name, description, instructions, provider, "
                "model_name, api_key, temperature, max_tokens, top_p, "
                "base_url, created_at, updated_at, owner_user_id, "
                "tools, skills, guardrails, memory_config, knowledge, "
                "attached_flows, use_subagents, source_template_id, sample_doc",
                (json.dumps(sample_doc or {}), now, agent_id, owner_user_id),
            ).fetchone()
        return _row_to_agent(row) if row else None

    return await asyncio.to_thread(_run)


async def clear_agent_sample_doc(
    agent_id: str,
    owner_user_id: str,
) -> Optional[Dict[str, Any]]:
    """Remove the sample-doc metadata from an agent row (sets to ``{}``).

    The on-disk file must be deleted by the caller — this function only
    touches the DB. Returns the updated agent row or ``None`` when the
    agent does not exist under this owner.
    """
    return await set_agent_sample_doc(agent_id, owner_user_id, {})
 
 
async def duplicate_agent(agent_id: str, owner_user_id: str) -> Optional[Dict[str, Any]]:
    import uuid
    original = await get_agent(agent_id, owner_user_id)
    if not original:
        return None
    # ``sample_doc`` is intentionally NOT carried over: the persisted
    # sample file lives under the original agent's id, so cloning the
    # dict here would make the copy silently share (and later
    # cross-delete) the same file. The duplicated agent starts without a
    # sample; the user can upload one for it if they still want the
    # look-and-feel guidance.
    copy_data = {k: v for k, v in original.items() if k not in (
        "id", "created_at", "updated_at", "owner_user_id", "sample_doc",
    )}
    copy_data["id"] = f"agent-{uuid.uuid4().hex[:12]}"
    copy_data["name"] = await asyncio.to_thread(
        _generate_unique_name, "agents", f"{original['name']} (Copy)", owner_user_id
    )
    result = await create_agent(copy_data, owner_user_id)
    if result:
        # Triggers are keyed by agent id; the duplicate gets a new id, so copy
        # the original's triggers onto it. Best-effort — a copy failure must not
        # fail the duplicate action.
        try:
            await _copy_agent_triggers(agent_id, result.get("id"), owner_user_id)
        except Exception:
            logger.exception("[AGENT] duplicate_agent: trigger copy failed for %s", agent_id)
    return result
 
 
# ---------------------------------------------------------------------------
# Tools / skills catalog (postgres-backed replacement for JSON registries)
# ---------------------------------------------------------------------------
 
def _row_to_tool(row) -> Dict[str, Any]:
    return {
        "name":         row[0],
        "description":  row[1],
        "input_schema": row[2] if row[2] is not None else {},
        "code":         row[3],
        "generated":    bool(row[4]),
        "service":      row[5] or "",
        "created_at":   row[6].isoformat() if row[6] else None,
        "updated_at":   row[7].isoformat() if row[7] else None,
    }
 
 
def _row_to_skill(row) -> Dict[str, Any]:
    # ``source`` (row[7]) was added after skills_catalog first shipped. Older
    # installs whose migration hasn't run yet return rows without it — fall
    # back to inferring from ``generated`` so callers never crash.
    src = row[7] if len(row) > 7 and row[7] else None
    if not src:
        src = "ai" if bool(row[4]) else "builtin"
    return {
        "name":        row[0],
        "description": row[1],
        "category":    row[2],
        "content":     row[3],
        "generated":   bool(row[4]),
        "created_at":  row[5].isoformat() if row[5] else None,
        "updated_at":  row[6].isoformat() if row[6] else None,
        "source":      src,
    }
 
 
_TOOL_SELECT = (
    "SELECT name, description, input_schema, code, generated, service, created_at, updated_at "
    "FROM tools_catalog"
)
_SKILL_SELECT = (
    "SELECT name, description, category, content, generated, created_at, updated_at, source "
    "FROM skills_catalog"
)

_DELETED_TOOL_CATALOG_SERVICES: Tuple[str, ...] = (
    "kb_search",
    "document_tools",
    "calendar_tools",
    "email_tools",
    "task_tracker",
    "data_tools",
    "ats_tools",
    "doc_generator",
    "translator",
    "lms_tools",
)
_DELETED_TOOL_CATALOG_NAMES: Tuple[str, ...] = (
    # Individual GitLab tools removed as duplicates / low-value niche tools.
    "gitlab_get_merge_request_diffs",
    "gitlab_list_mr_notes",
    "gitlab_unapprove_merge_request",
    "gitlab_list_namespaces",
    "gitlab_list_registry_repositories",
    "gitlab_list_registry_tags",
    "gitlab_get_file_metadata",
    "gitlab_cancel_job",
    # Individual Jira tools removed as duplicates / low-value niche tools.
    "jira_get_issue_dict",
    "jira_create_if",
    "jira_create_if_stale",
    "jira_okr_list",
    "jira_update_worklog",
    "jira_log_agent_action",
    # Round 2 — admin/config/niche clusters + overlapping wrappers removed.
    # GitLab:
    "gitlab_comment_on_mr",
    "gitlab_protect_branch",
    "gitlab_unprotect_branch",
    "gitlab_list_project_members",
    "gitlab_list_webhooks",
    "gitlab_create_webhook",
    "gitlab_get_group",
    "gitlab_list_group_members",
    "gitlab_list_group_projects",
    # Jira:
    "jira_transition_issue",
    "jira_assign",
    "jira_list_worklogs",
    "jira_add_worklog",
    "jira_list_boards",
    "jira_list_sprints",
    "jira_get_sprint",
    "jira_move_issues_to_sprint",
    "jira_list_versions",
    "jira_create_version",
    "jira_update_version",
    "jira_list_components",
    "jira_create_component",
    "jira_update_component",
    "jira_list_statuses",
    "jira_list_fields",
    "jira_list_priorities",
)
_DELETED_TOOL_CATALOG_PREFIXES: Tuple[str, ...] = tuple(
    f"{service}__" for service in _DELETED_TOOL_CATALOG_SERVICES
)


# ---- Tools ----------------------------------------------------------------

async def purge_deleted_tool_catalog_rows() -> int:
    """Remove stale catalog rows for deleted in-repo MCP tool families and
    individually removed tools."""
    _require_uri()
    service_placeholders = ", ".join(["%s"] * len(_DELETED_TOOL_CATALOG_SERVICES))
    prefix_clauses = " OR ".join(["starts_with(name, %s)"] * len(_DELETED_TOOL_CATALOG_PREFIXES))
    name_placeholders = ", ".join(["%s"] * len(_DELETED_TOOL_CATALOG_NAMES))
    params = (
        *_DELETED_TOOL_CATALOG_SERVICES,
        *_DELETED_TOOL_CATALOG_PREFIXES,
        *_DELETED_TOOL_CATALOG_NAMES,
    )

    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute(
                f"DELETE FROM tools_catalog "
                f"WHERE service IN ({service_placeholders}) "
                f"OR {prefix_clauses} "
                f"OR name IN ({name_placeholders})",
                params,
            )
            conn.commit()
            return cur.rowcount or 0
    return await asyncio.to_thread(_run)


async def get_tool(name: str) -> Optional[Dict[str, Any]]:
    """Look up a tool by name. Served from the in-process cache (REQ-P3-1)
    after the first read; a writer (``upsert_tool`` / ``delete_tool`` /
    ``clear_all_tools``) invalidates the relevant entry so this never
    returns stale data across an edit -- see ``_cached_catalog_read`` for
    the generation-guarded race fix and the deep-copy isolation."""
    _require_uri()

    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(f"{_TOOL_SELECT} WHERE name = %s", (name,)).fetchone()
        return _row_to_tool(row) if row else None

    return await _cached_catalog_read(
        _tool_cache, _tool_cache_generation, name,
        lambda: asyncio.to_thread(_run),
    )
 
 
async def list_tools() -> List[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            rows = conn.execute(f"{_TOOL_SELECT} ORDER BY name").fetchall()
        return [_row_to_tool(r) for r in rows]
    return await asyncio.to_thread(_run)
 
 
async def upsert_tool(
    name: str,
    code: str,
    description: str = "",
    input_schema: Optional[Dict[str, Any]] = None,
    generated: bool = True,
    service: str = "",
) -> Dict[str, Any]:
    """Insert or update a tool by name. Returns the persisted row."""
    _require_uri()
    schema_json = json.dumps(input_schema or {})
    now = datetime.now(timezone.utc)
 
    def _run():
        with _get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO tools_catalog "
                "(name, description, input_schema, code, generated, service, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET "
                "  description = EXCLUDED.description, "
                "  input_schema = EXCLUDED.input_schema, "
                "  code = EXCLUDED.code, "
                "  generated = EXCLUDED.generated, "
                "  service = EXCLUDED.service, "
                "  updated_at = EXCLUDED.updated_at",
                (name, description, schema_json, code, generated, service, now, now),
            )
            conn.commit()
            row = conn.execute(f"{_TOOL_SELECT} WHERE name = %s", (name,)).fetchone()
        return _row_to_tool(row)
    result = await asyncio.to_thread(_run)
    _invalidate_tool_cache(name)
    return result
 
 
async def delete_tool(name: str) -> bool:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute("DELETE FROM tools_catalog WHERE name = %s", (name,))
            conn.commit()
            return cur.rowcount > 0
    result = await asyncio.to_thread(_run)
    _invalidate_tool_cache(name)
    return result
 
 
async def clear_all_tools() -> int:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute("DELETE FROM tools_catalog")
            conn.commit()
            return cur.rowcount or 0
    result = await asyncio.to_thread(_run)
    _invalidate_tool_cache()
    return result
 
 
# ---- Skills ---------------------------------------------------------------
 
async def get_skill(name: str) -> Optional[Dict[str, Any]]:
    """Look up a skill by name. Served from the in-process cache (REQ-P3-1)
    after the first read; invalidated by ``upsert_skill`` / ``delete_skill`` /
    ``seed_skill_if_not_exists`` -- see ``_cached_catalog_read`` for the
    generation-guarded race fix and the deep-copy isolation."""
    _require_uri()

    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(f"{_SKILL_SELECT} WHERE name = %s", (name,)).fetchone()
        return _row_to_skill(row) if row else None

    return await _cached_catalog_read(
        _skill_cache, _skill_cache_generation, name,
        lambda: asyncio.to_thread(_run),
    )
 
 
async def list_skills() -> List[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            rows = conn.execute(f"{_SKILL_SELECT} ORDER BY name").fetchall()
        return [_row_to_skill(r) for r in rows]
    return await asyncio.to_thread(_run)
 
 
async def seed_skill_if_not_exists(
    name: str,
    content: str,
    description: str = "",
    category: str = "general",
) -> bool:
    """Insert a skill only if it doesn't already exist. Returns True if inserted."""
    _require_uri()
    now = datetime.now(timezone.utc)
 
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute(
                "INSERT INTO skills_catalog "
                "(name, description, category, content, generated, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, FALSE, %s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (name, description, category, content, now, now),
            )
            conn.commit()
            return cur.rowcount > 0
    result = await asyncio.to_thread(_run)
    # Only invalidate when a row was actually inserted -- ON CONFLICT DO
    # NOTHING means a no-op call (the common case on every repeated
    # seed/startup pass once the skill already exists) must not flush a
    # hot cache entry for nothing. Ties invalidation to an actual mutation,
    # matching every other mutator in this module.
    if result:
        _invalidate_skill_cache(name)
    return result
 
 
async def upsert_skill(
    name: str,
    content: str,
    description: str = "",
    category: str = "general",
    generated: bool = True,
    source: str | None = None,
) -> Dict[str, Any]:
    """Insert or update a skill by name. Returns the persisted row.

    ``source`` tags the origin of the row for the Skills-tab filter/badge:
    ``builtin`` / ``ai`` / ``upload``. When left as ``None`` we preserve the
    stored value on an UPDATE (so an edit doesn't silently reclassify an
    Uploaded skill as AI Generated); brand-new INSERTs without an explicit
    source default to ``ai`` — that matches the pre-upload era when the only
    user-authored skills came from the Skill Factory.
    """
    _require_uri()
    now = datetime.now(timezone.utc)
    normalized_source = source if source in ("builtin", "ai", "upload") else None
 
    def _run():
        with _get_pool().connection() as conn:
            # Two variants of the UPSERT: with source (INSERT+overwrite) and
            # without (INSERT default, keep existing on UPDATE). Splitting the
            # query keeps ``COALESCE(EXCLUDED, existing)`` out of the SQL and
            # makes the "preserve on edit" behaviour explicit at the call site.
            if normalized_source is not None:
                conn.execute(
                    "INSERT INTO skills_catalog "
                    "(name, description, category, content, generated, source, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "  description = EXCLUDED.description, "
                    "  category = EXCLUDED.category, "
                    "  content = EXCLUDED.content, "
                    "  generated = EXCLUDED.generated, "
                    "  source = EXCLUDED.source, "
                    "  updated_at = EXCLUDED.updated_at",
                    (name, description, category, content, generated, normalized_source, now, now),
                )
            else:
                # No source hint from the caller — preserve whatever's stored
                # on UPDATE; use the column default ('ai') on fresh INSERT.
                conn.execute(
                    "INSERT INTO skills_catalog "
                    "(name, description, category, content, generated, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "  description = EXCLUDED.description, "
                    "  category = EXCLUDED.category, "
                    "  content = EXCLUDED.content, "
                    "  generated = EXCLUDED.generated, "
                    "  updated_at = EXCLUDED.updated_at",
                    (name, description, category, content, generated, now, now),
                )
            conn.commit()
            row = conn.execute(f"{_SKILL_SELECT} WHERE name = %s", (name,)).fetchone()
        return _row_to_skill(row)
    result = await asyncio.to_thread(_run)
    _invalidate_skill_cache(name)
    return result
 
 
async def delete_skill(name: str) -> bool:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute("DELETE FROM skills_catalog WHERE name = %s", (name,))
            conn.commit()
            return cur.rowcount > 0
    result = await asyncio.to_thread(_run)
    _invalidate_skill_cache(name)
    return result
 
 
# ---- Skill files (progressive disclosure) --------------------------------
#
# Each row is one bundled file from a skill folder (e.g. pptx/pythonpptx.md,
# pptx/scripts/thumbnail.py). The LLM pulls these on demand via the
# read_skill_file tool instead of having them inlined in the system prompt.
 
_SKILL_FILE_SELECT = (
    "SELECT skill_name, rel_path, content, size_bytes, description, "
    "kind, abs_path, updated_at "
    "FROM skill_files"
)
 
 
def _row_to_skill_file(row) -> Dict[str, Any]:
    return {
        "skill_name":  row[0],
        "rel_path":    row[1],
        "content":     row[2],
        "size_bytes":  row[3],
        "description": row[4],
        "kind":        row[5],
        "abs_path":    row[6],
        "updated_at":  row[7].isoformat() if row[7] else None,
    }
 
 
async def upsert_skill_files(
    skill_name: str, files: List[Dict[str, Any]],
) -> int:
    """Replace the set of bundled files for ``skill_name`` with ``files``.
 
    Idempotent — rows whose ``rel_path`` is not in ``files`` get deleted so
    files removed from disk stop appearing in the manifest after restart.
 
    Each entry in ``files`` must have: rel_path, content, size_bytes,
    description, kind ('reference' | 'script'), abs_path.
 
    Returns the number of rows written.
    """
    _require_uri()
    now = datetime.now(timezone.utc)
 
    def _run():
        with _get_pool().connection() as conn:
            keep_paths = [f["rel_path"] for f in files]
            if keep_paths:
                placeholders = ",".join(["%s"] * len(keep_paths))
                conn.execute(
                    "DELETE FROM skill_files WHERE skill_name = %s "
                    f"AND rel_path NOT IN ({placeholders})",
                    (skill_name, *keep_paths),
                )
            else:
                conn.execute(
                    "DELETE FROM skill_files WHERE skill_name = %s",
                    (skill_name,),
                )
 
            written = 0
            for f in files:
                conn.execute(
                    "INSERT INTO skill_files "
                    "(skill_name, rel_path, content, size_bytes, description, "
                    "kind, abs_path, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (skill_name, rel_path) DO UPDATE SET "
                    "  content = EXCLUDED.content, "
                    "  size_bytes = EXCLUDED.size_bytes, "
                    "  description = EXCLUDED.description, "
                    "  kind = EXCLUDED.kind, "
                    "  abs_path = EXCLUDED.abs_path, "
                    "  updated_at = EXCLUDED.updated_at",
                    (
                        skill_name,
                        f["rel_path"],
                        f["content"],
                        int(f["size_bytes"]),
                        f.get("description", ""),
                        f["kind"],
                        f["abs_path"],
                        now,
                    ),
                )
                written += 1
            conn.commit()
            return written
 
    result = await asyncio.to_thread(_run)
    _invalidate_skill_cache(skill_name)
    return result
 
 
async def list_skill_files(skill_name: str) -> List[Dict[str, Any]]:
    """Return all bundled files for ``skill_name`` ordered by rel_path.

    Used by the manifest renderer — it does NOT return ``content`` to keep
    the prompt small. The LLM pulls content on demand via read_skill_file.

    Served from the in-process cache (REQ-P3-1) after the first read;
    invalidated by ``upsert_skill_files`` and the skill mutators. Shares
    ``_skill_cache_generation`` with ``get_skill`` since
    ``_invalidate_skill_cache`` always evicts both together -- see
    ``_cached_catalog_read`` for the generation-guarded race fix and the
    deep-copy isolation.
    """
    _require_uri()

    def _run():
        with _get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT skill_name, rel_path, '', size_bytes, description, "
                "kind, abs_path, updated_at "
                "FROM skill_files WHERE skill_name = %s ORDER BY rel_path",
                (skill_name,),
            ).fetchall()
        return [_row_to_skill_file(r) for r in rows]

    return await _cached_catalog_read(
        _skill_files_cache, _skill_cache_generation, skill_name,
        lambda: asyncio.to_thread(_run),
    )
 
 
async def get_skill_file(
    skill_name: str, rel_path: str,
) -> Optional[Dict[str, Any]]:
    """Fetch a single bundled file by (skill_name, rel_path)."""
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                f"{_SKILL_FILE_SELECT} WHERE skill_name = %s AND rel_path = %s",
                (skill_name, rel_path),
            ).fetchone()
        return _row_to_skill_file(row) if row else None
    return await asyncio.to_thread(_run)
 
 
async def count_tools() -> int:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM tools_catalog").fetchone()[0]
    return await asyncio.to_thread(_run)
 
 
async def count_skills() -> int:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM skills_catalog").fetchone()[0]
    return await asyncio.to_thread(_run)
 
 
# ---------------------------------------------------------------------------
# Triggers (Routines) — scheduled workflow / agent execution
# ---------------------------------------------------------------------------
 
def _row_to_trigger(row) -> Dict[str, Any]:
    return {
        "id":          row[0],
        "target_kind": row[1],
        "target_id":   row[2],
        "name":        row[3],
        "schedule":    row[4] if row[4] is not None else {},
        "input_text":  row[5],
        "enabled":     bool(row[6]),
        "owner_user_id": row[7],
        "created_at":  row[8].isoformat() if row[8] else None,
        "updated_at":  row[9].isoformat() if row[9] else None,
        "next_run_at": row[10].isoformat() if row[10] else None,
        "last_run_at": row[11].isoformat() if row[11] else None,
        "last_status": row[12],
        "node_id":     row[13],
    }
 
 
_TRIGGER_SELECT = (
    "SELECT id, target_kind, target_id, name, schedule, input_text, enabled, "
    "owner_user_id, created_at, updated_at, next_run_at, last_run_at, last_status, "
    "node_id "
    "FROM triggers"
)
 
 
async def create_trigger(data: Dict[str, Any], owner_user_id: str) -> Dict[str, Any]:
    _require_uri()
    import uuid
    trigger_id = data.get("id") or f"trigger-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
 
    def _run():
        with _get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO triggers "
                "(id, target_kind, target_id, node_id, name, schedule, input_text, enabled, "
                "owner_user_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    trigger_id,
                    data["target_kind"],
                    data["target_id"],
                    data.get("node_id") or None,
                    data.get("name", "") or "",
                    json.dumps(data.get("schedule") or {}),
                    data.get("input_text", "") or "",
                    bool(data.get("enabled", True)),
                    owner_user_id,
                    now, now,
                ),
            )
            conn.commit()
            row = conn.execute(
                f"{_TRIGGER_SELECT} WHERE id = %s",
                (trigger_id,),
            ).fetchone()
        return _row_to_trigger(row)
    return await asyncio.to_thread(_run)
 
 
async def get_trigger(trigger_id: str, owner_user_id: str) -> Optional[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                f"{_TRIGGER_SELECT} WHERE id = %s AND owner_user_id = %s",
                (trigger_id, owner_user_id),
            ).fetchone()
        return _row_to_trigger(row) if row else None
    return await asyncio.to_thread(_run)
 
 
async def get_trigger_by_id(trigger_id: str) -> Optional[Dict[str, Any]]:
    """Look up a trigger without owner filtering (used by the scheduler)."""
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                f"{_TRIGGER_SELECT} WHERE id = %s",
                (trigger_id,),
            ).fetchone()
        return _row_to_trigger(row) if row else None
    return await asyncio.to_thread(_run)
 
 
async def list_triggers(
    owner_user_id: str,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    node_id: Optional[str] = None,
    node_scope: str = "any",
) -> List[Dict[str, Any]]:
    """List triggers for the user, optionally filtered by target.
 
    ``node_scope`` controls how ``node_id`` is interpreted:
      * "any"             — ignore node_id (return every row matching kind+id)
      * "exact"           — return only rows whose ``node_id`` equals the arg
                            (use empty string / None to match rows with NULL)
      * "workflow_only"   — return only rows with ``node_id IS NULL``
                            (workflow-wide triggers, no node binding)
    """
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            params: List[Any] = [owner_user_id]
            sql = f"{_TRIGGER_SELECT} WHERE owner_user_id = %s"
            if target_kind and target_id:
                sql += " AND target_kind = %s AND target_id = %s"
                params.extend([target_kind, target_id])
            if node_scope == "exact":
                if node_id:
                    sql += " AND node_id = %s"
                    params.append(node_id)
                else:
                    sql += " AND node_id IS NULL"
            elif node_scope == "workflow_only":
                sql += " AND node_id IS NULL"
            sql += " ORDER BY created_at DESC"
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_trigger(r) for r in rows]
    return await asyncio.to_thread(_run)
 
 
async def list_all_enabled_triggers() -> List[Dict[str, Any]]:
    """Used by the scheduler at startup to re-register every active job."""
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            rows = conn.execute(
                f"{_TRIGGER_SELECT} WHERE enabled = TRUE"
            ).fetchall()
        return [_row_to_trigger(r) for r in rows]
    return await asyncio.to_thread(_run)
 
 
async def update_trigger(
    trigger_id: str, data: Dict[str, Any], owner_user_id: str
) -> Optional[Dict[str, Any]]:
    _require_uri()
    now = datetime.now(timezone.utc)
 
    def _run():
        with _get_pool().connection() as conn:
            fields, values = [], []
            if "name" in data and data["name"] is not None:
                fields.append("name = %s"); values.append(data["name"])
            if "schedule" in data and data["schedule"] is not None:
                fields.append("schedule = %s"); values.append(json.dumps(data["schedule"]))
            if "input_text" in data and data["input_text"] is not None:
                fields.append("input_text = %s"); values.append(data["input_text"])
            if "enabled" in data and data["enabled"] is not None:
                fields.append("enabled = %s"); values.append(bool(data["enabled"]))
            fields.append("updated_at = %s"); values.append(now)
            values.extend([trigger_id, owner_user_id])
            conn.execute(
                f"UPDATE triggers SET {', '.join(fields)} "
                f"WHERE id = %s AND owner_user_id = %s",
                values,
            )
            conn.commit()
            row = conn.execute(
                f"{_TRIGGER_SELECT} WHERE id = %s AND owner_user_id = %s",
                (trigger_id, owner_user_id),
            ).fetchone()
        return _row_to_trigger(row) if row else None
    return await asyncio.to_thread(_run)
 
 
async def update_trigger_run_metadata(
    trigger_id: str,
    next_run_at: Optional[datetime] = None,
    last_run_at: Optional[datetime] = None,
    last_status: Optional[str] = None,
    clear_next_run_at: bool = False,
) -> None:
    """Update per-fire book-keeping columns on a triggers row.

    ``clear_next_run_at=True`` writes ``NULL`` to next_run_at. It exists so the
    "toggle enabled → disabled" and "past once schedule" paths can positively
    clear the column instead of leaving a stale future timestamp behind (which
    would mislead the UI's "Next run" display and, after re-enable, would let
    the dispatcher fire immediately at the stale time). The plain
    ``next_run_at=None`` no-op semantics is preserved for the common case
    where the caller only wants to write last_run_at / last_status.
    """
    _require_uri()
 
    def _run():
        with _get_pool().connection() as conn:
            fields, values = [], []
            if clear_next_run_at:
                fields.append("next_run_at = NULL")
            elif next_run_at is not None:
                fields.append("next_run_at = %s"); values.append(next_run_at)
            if last_run_at is not None:
                fields.append("last_run_at = %s"); values.append(last_run_at)
            if last_status is not None:
                fields.append("last_status = %s"); values.append(last_status)
            if not fields:
                return
            values.append(trigger_id)
            conn.execute(
                f"UPDATE triggers SET {', '.join(fields)} WHERE id = %s",
                values,
            )
            conn.commit()
    await asyncio.to_thread(_run)
 
 
async def delete_trigger(trigger_id: str, owner_user_id: str) -> bool:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM triggers WHERE id = %s AND owner_user_id = %s",
                (trigger_id, owner_user_id),
            )
            conn.commit()
            return cur.rowcount > 0
    return await asyncio.to_thread(_run)
 
 
async def delete_triggers_for_target(target_kind: str, target_id: str) -> int:
    """Best-effort cleanup when the workflow/agent itself is deleted."""
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM triggers WHERE target_kind = %s AND target_id = %s",
                (target_kind, target_id),
            )
            conn.commit()
            return cur.rowcount or 0
    return await asyncio.to_thread(_run)
 
 
# ---- Trigger executions ---------------------------------------------------
 
def _row_to_execution(row) -> Dict[str, Any]:
    return {
        "id":          row[0],
        "trigger_id":  row[1],
        "target_kind": row[2],
        "target_id":   row[3],
        "target_name": row[4],
        "started_at":  row[5].isoformat() if row[5] else None,
        "finished_at": row[6].isoformat() if row[6] else None,
        "status":      row[7],
        "input_text":  row[8],
        "output":      row[9],
        "error":       row[10],
        "seen":        bool(row[11]),
        "generated_files": row[12] if len(row) > 12 and row[12] is not None else [],
    }
 
 
_EXEC_SELECT = (
    "SELECT id, trigger_id, target_kind, target_id, target_name, "
    "started_at, finished_at, status, input_text, output, error, seen, "
    "generated_files "
    "FROM trigger_executions"
)
 
 
async def insert_trigger_execution(
    trigger_id: str,
    target_kind: str,
    target_id: str,
    target_name: str,
    input_text: str,
    owner_user_id: str,
) -> int:
    _require_uri()
    now = datetime.now(timezone.utc)
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO trigger_executions "
                "(trigger_id, target_kind, target_id, target_name, started_at, "
                "status, input_text, owner_user_id) "
                "VALUES (%s, %s, %s, %s, %s, 'running', %s, %s) RETURNING id",
                (trigger_id, target_kind, target_id, target_name, now,
                 input_text, owner_user_id),
            ).fetchone()
            conn.commit()
            return int(row[0])
    return await asyncio.to_thread(_run)
 
 
async def finalize_trigger_execution(
    execution_id: int,
    status: str,
    output: Optional[str] = None,
    error: Optional[str] = None,
    generated_files: Optional[List[Dict[str, Any]]] = None,
) -> None:
    _require_uri()
    now = datetime.now(timezone.utc)
    gf_json = json.dumps(generated_files or [])
    def _run():
        with _get_pool().connection() as conn:
            conn.execute(
                "UPDATE trigger_executions SET finished_at = %s, status = %s, "
                "output = %s, error = %s, generated_files = %s WHERE id = %s",
                (now, status, output, error, gf_json, execution_id),
            )
            conn.commit()
    await asyncio.to_thread(_run)
 
 
async def list_trigger_executions(
    owner_user_id: str,
    trigger_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            if trigger_id:
                rows = conn.execute(
                    f"{_EXEC_SELECT} WHERE owner_user_id = %s AND trigger_id = %s "
                    f"ORDER BY started_at DESC LIMIT %s",
                    (owner_user_id, trigger_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"{_EXEC_SELECT} WHERE owner_user_id = %s "
                    f"ORDER BY started_at DESC LIMIT %s",
                    (owner_user_id, limit),
                ).fetchall()
        return [_row_to_execution(r) for r in rows]
    return await asyncio.to_thread(_run)
 
 
async def list_unseen_executions(owner_user_id: str) -> List[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            rows = conn.execute(
                f"{_EXEC_SELECT} WHERE owner_user_id = %s AND seen = FALSE "
                f"AND status IN ('success', 'error') "
                f"ORDER BY started_at DESC",
                (owner_user_id,),
            ).fetchall()
        return [_row_to_execution(r) for r in rows]
    return await asyncio.to_thread(_run)
 
 
async def mark_execution_seen(execution_id: int, owner_user_id: str) -> bool:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE trigger_executions SET seen = TRUE "
                "WHERE id = %s AND owner_user_id = %s",
                (execution_id, owner_user_id),
            )
            conn.commit()
            return cur.rowcount > 0
    return await asyncio.to_thread(_run)
 
 
async def mark_all_executions_seen(owner_user_id: str) -> int:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE trigger_executions SET seen = TRUE "
                "WHERE owner_user_id = %s AND seen = FALSE",
                (owner_user_id,),
            )
            conn.commit()
            return cur.rowcount or 0
    return await asyncio.to_thread(_run)
 
 
async def get_execution(execution_id: int, owner_user_id: str) -> Optional[Dict[str, Any]]:
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            row = conn.execute(
                f"{_EXEC_SELECT} WHERE id = %s AND owner_user_id = %s",
                (execution_id, owner_user_id),
            ).fetchone()
        return _row_to_execution(row) if row else None
    return await asyncio.to_thread(_run)
 
 
async def delete_execution(execution_id: int, owner_user_id: str) -> bool:
    """Hard-delete a single trigger execution row (audit log entry)."""
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM trigger_executions "
                "WHERE id = %s AND owner_user_id = %s",
                (execution_id, owner_user_id),
            )
            conn.commit()
            return cur.rowcount > 0
    return await asyncio.to_thread(_run)
 
 
async def delete_all_executions(owner_user_id: str) -> int:
    """Hard-delete every trigger execution row owned by the user."""
    _require_uri()
    def _run():
        with _get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM trigger_executions WHERE owner_user_id = %s",
                (owner_user_id,),
            )
            conn.commit()
            return cur.rowcount or 0
    return await asyncio.to_thread(_run)
 
 
# ---------------------------------------------------------------------------
# Integration credentials — resolved from the main platform's API Token Vault
# (user_tokens table) via core/platform_credentials.py.
# ---------------------------------------------------------------------------
 
 
async def get_all_connection_env_vars(user_id: str = "", email: str = "") -> Dict[str, str]:
    """Return integration credentials as env vars for subprocess injection.

    GitLab and Atlassian (Jira/Confluence) authentication uses the requesting
    user's OWN token ONLY — resolved from the ``user_tokens`` table via
    ``core/platform_credentials`` (looked up by user_id or email). There is NO
    platform/service-account fallback: if the user has not configured a token,
    the corresponding env var is simply left unset and the downstream tool raises
    a clear "not configured" error.

    Org-level Credential Vault (``store/credential_vault``) still supplies OTHER
    named credentials as upper-cased env vars (e.g. ``slack_api_key`` →
    ``SLACK_API_KEY``), but it is explicitly forbidden from setting any managed
    integration credential key (GITLAB_TOKEN, JIRA_*, CONFLUENCE_*) so it can
    never reintroduce a shared token.
    """
    env: Dict[str, str] = {}

    # ── 0. Always export GITLAB_URL so canonical gitlab tools and the
    # code_executor subagent both target the right GitLab instance even
    # when the backend was started without it in os.environ. .env values
    # still win because they're loaded into os.environ before this fires,
    # and os.environ is merged AFTER this dict at the subprocess layer
    # (see ToolDispatcher._run_in_sandbox).
    env["GITLAB_URL"] = os.environ.get("GITLAB_URL", "https://<YOUR_GITLAB_URL>")

    # ── 1. Per-user tokens (GitLab, Atlassian) from user_tokens table ────
    try:
        from core.platform_credentials import (
            get_gitlab_token, get_atlassian_creds, extract_gitlab_pat,
        )

        try:
            # The stored value may be a bare PAT or ``username:token`` /
            # ``username@token`` (the latter so it can be baked into a clone
            # URL). The GitLab REST API ``PRIVATE-TOKEN`` header (used by the
            # sandbox gitlab tools) needs the BARE token, so normalize here.
            # No shape-validation / rejection: an invalid token is passed
            # through so the GitLab API returns an authoritative 401 rather than
            # us silently dropping a legitimate token. There is NO platform
            # fallback — if the user has no token, get_gitlab_token raises and
            # GITLAB_TOKEN is left unset.
            token = get_gitlab_token(user_id=user_id, email=email)
            pat = extract_gitlab_pat(token)
            if pat:
                env["GITLAB_TOKEN"] = pat
        except PermissionError:
            pass  # user hasn't configured a GitLab token — leave unset (no fallback)
 
        try:
            atlassian_email, atlassian_token = get_atlassian_creds(user_id=user_id, email=email)
            env["JIRA_EMAIL"] = atlassian_email
            env["JIRA_API_TOKEN"] = atlassian_token
        except PermissionError:
            pass  # user hasn't configured an Atlassian token — skip silently
    except ImportError:
        pass  # platform_credentials not available (standalone deployment)
 
    # ── 2. Org-level credentials from the Credential Vault ───────────────
    try:
        from store.credential_vault import list_credentials, get_credential_value
        from core.platform_credentials import MANAGED_CREDENTIAL_ENV_KEYS

        for cred in list_credentials():
            name = cred.get("name", "")
            if not name:
                continue
            # Normalise to a valid env-var name: upper-case, hyphens/spaces → underscores
            env_key = name.upper().replace("-", "_").replace(" ", "_")
            # NEVER let the shared org vault supply a managed integration
            # credential (GitLab/Jira/Confluence auth). Those must be per-user
            # only — a vault entry here would be a platform-level fallback.
            if env_key in MANAGED_CREDENTIAL_ENV_KEYS:
                continue
            # Skip if a per-user token already occupies this key
            if env_key in env:
                continue
            try:
                value = get_credential_value(name)
                if value:
                    env[env_key] = value
            except Exception:
                pass  # individual credential decrypt failure — skip
    except Exception:
        pass  # vault unavailable (standalone or DB down) — skip silently
 
    return env
 