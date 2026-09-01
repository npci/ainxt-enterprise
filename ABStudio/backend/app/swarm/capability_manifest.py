# SPDX-License-Identifier: Apache-2.0
"""Grounded catalog the orchestrator must plan against.

The whole point of this layer is to KILL hallucinated tool names. The
orchestrator LLM only sees tools/skills/KBs that actually exist in this
deployment. Any plan that references a name NOT in the manifest is
rejected by ``validate_against_manifest`` before a single worker spawns.

Sources of truth (must match what ``AgentRunner`` can actually execute):

* tools  — ``workflow_repo.list_tools()`` reads the postgres
  ``tools_catalog`` table. We use the REPO call, not the in-memory
  ``CANONICAL_TOOLS`` constant, because the constant omits user-generated
  tools and drafts.

* skills — ``workflow_repo.list_skills()`` reads ``skills_catalog``.

* KBs    — ``kb_retriever._all_docs_kb_repos()`` enumerates every
  ``docs_kb:*`` namespace. ACL is enforced downstream inside
  ``pgvector_search`` at retrieval time, so we surface every namespace in
  the manifest — workers that get assigned an inaccessible KB will simply
  get empty retrieval and report low confidence. Filtering at manifest
  time would require a per-user ACL pre-check helper that doesn't exist
  in v1.

Cache: process-local, 60s TTL by default (env ``SWARM_MANIFEST_TTL_S``).
Mirrors the pattern at ``kb_retriever._all_docs_kb_repos`` (60s TTL),
which is the closest precedent in the codebase.

The renderer is character-budgeted (no token counting in v1 — there is no
``tiktoken`` dependency). 6000 chars ≈ 1500 tokens, which leaves the
orchestrator plenty of room for plan output.
"""
from __future__ import annotations

import asyncio
import difflib

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.logger import logger
# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------

_MANIFEST_TTL_S = int(os.getenv("SWARM_MANIFEST_TTL_S", "60"))
# Default lowered from 32000 → 16000 chars. Since
# ``_format_concise_tool_line`` emits ``name + (required: ...) + summary``
# only (full 180-char descriptions are never rendered — they live on the
# canonical ``tools_catalog`` row and are hydrated by the worker at
# execute time), 16000 chars (~4000 tokens) is enough for the full 148+
# tool catalog grouped by service. Halving the budget leaves the
# orchestrator more headroom under its ``SWARM_ORCHESTRATOR_MAX_TOKENS``
# response cap and makes plan-output bloat the dominant token-budget
# question, not manifest input. Override via env if a deployment
# publishes far more tools.
_MAX_MANIFEST_CHARS = int(os.getenv("SWARM_MANIFEST_MAX_CHARS", "16000"))
_PER_ITEM_CHAR_CAP = int(os.getenv("SWARM_MANIFEST_PER_ITEM_CHARS", "180"))


def _sort_by_parent_order(
    tools: List[Dict[str, Any]],
    parent_attached_tools: Iterable[str],
) -> List[Dict[str, Any]]:
    """Sort ``tools`` to match ``parent_attached_tools`` order.

    Tools whose ``name`` is not in the parent list sort to the end. Used
    at two sites: strict-scope manifest filtering and the [PARENT-ATTACHED
    TOOLS] renderer — both need the same ordering so the planner sees a
    stable, user-controlled sequence.
    """
    order = {n: i for i, n in enumerate(parent_attached_tools)}
    return sorted(
        tools,
        key=lambda t: order.get(t.get("name", ""), 1_000_000),
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

# Keyed by (user_id, email) so a future per-user catalog (e.g. a tool that
# only appears for admins) caches per-identity. For now both fields are
# essentially ignored by the upstream enumerators, so the cache key is
# effectively global — but the shape is right for the day we wire ACL in.
_cache: Dict[Tuple[str, str], Tuple[float, "CapabilityManifest"]] = {}
_cache_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Public type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityManifest:
    """Snapshot of the local catalog as of ``built_at``.

    Workers and orchestrator plans are validated against THIS snapshot,
    not the live DB. That means a tool added 30s ago is invisible until
    the cache TTL expires — which is the same staleness window
    ``kb_retriever`` accepts for its own catalog.
    """
    tools:    Tuple[Dict[str, Any], ...]  # each: {name, description, service}
    skills:   Tuple[Dict[str, Any], ...]  # each: {name, description, category}
    kb_ids:   Tuple[str, ...]
    built_at: float                        = field(default_factory=lambda: 0.0)

    @property
    def tool_names(self) -> set:
        return {t["name"] for t in self.tools if t.get("name")}

    @property
    def skill_names(self) -> set:
        return {s["name"] for s in self.skills if s.get("name")}

    @property
    def kb_id_set(self) -> set:
        return set(self.kb_ids)

    # ------------------------------------------------------------------
    # Builder
    # ------------------------------------------------------------------
    @classmethod
    async def build(cls, user_id: str = "", email: str = "",
                    *, force_refresh: bool = False) -> "CapabilityManifest":
        """Return a cached (or freshly built) snapshot.

        ``force_refresh=True`` bypasses the cache — useful in tests and
        for an eventual admin "rebuild now" button. Production calls
        leave it False so the orchestrator hot path stays cheap.
        """
        key = (user_id or "", email or "")
        now = time.monotonic()

        if not force_refresh:
            entry = _cache.get(key)
            if entry is not None and entry[0] > now:
                return entry[1]

        async with _cache_lock:
            # Re-check under the lock — another coroutine may have
            # rebuilt the manifest while we were waiting.
            if not force_refresh:
                entry = _cache.get(key)
                if entry is not None and entry[0] > now:
                    return entry[1]

            manifest = await cls._build_uncached(user_id, email)
            # Compute expiry from POST-build wall so a slow build doesn't
            # silently shorten the next TTL window.
            expiry = time.monotonic() + _MANIFEST_TTL_S
            # Lazy eviction: drop other entries that have expired while
            # we were building. Cheap (≤ N user keys), and bounds memory
            # in multi-tenant deployments so the dict can't grow
            # unboundedly across the process lifetime.
            _evict_expired_locked(time.monotonic())
            _cache[key] = (expiry, manifest)
            return manifest

    @classmethod
    async def _build_uncached(cls, user_id: str, email: str) -> "CapabilityManifest":
        """Fetch tools/skills/KBs from the live repos in parallel.

        Each fetch runs concurrently via ``asyncio.gather`` — they're
        independent DB/cache calls so the cache-miss latency is bounded
        by the slowest, not the sum. Per-source failures are swallowed
        into empty lists with a logged warning; the orchestrator can
        still plan against whichever sub-catalog did come back, and an
        empty manifest forces it to return a single "I cannot do
        anything" worker — the right degradation.
        """
        from app.core import workflow_repo as _wf_repo
        from app.core import kb_retriever

        async def _safe(coro_fn, label):
            try:
                return await coro_fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f'[AGENT] swarm manifest: {label} failed: {exc}')
                return []

        raw_tools, raw_skills, raw_kbs = await asyncio.gather(
            _safe(_wf_repo.list_tools, "list_tools"),
            _safe(_wf_repo.list_skills, "list_skills"),
            _safe(kb_retriever._all_docs_kb_repos, "kb enumeration"),
        )

        tools: List[Dict[str, Any]] = []
        for t in raw_tools or []:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            full_desc = t.get("description") or ""
            input_schema = t.get("input_schema") or {}
            # SWARM-ONLY in-memory projection. The orchestrator picks
            # tool names by reading a CONCISE catalog (name + one-line
            # summary + required-params hint). The worker still executes
            # tools with the full ``description`` + ``input_schema`` from
            # the canonical ``tools_catalog`` row (Fix #1 hydration in
            # AgentRunner._load_agent). Parent agents, MCP, connectors,
            # and workflow flows continue to read the catalog row
            # directly — these fields exist only in this swarm-scoped
            # manifest snapshot, never persisted.
            #
            # We deliberately DO NOT persist the full description in the
            # projection: ``_format_concise_tool_line`` only reads
            # ``summary`` (with a back-compat ``description`` fallback),
            # so carrying the 180-char description doubles the dict size
            # for no rendering benefit and risks an accidental future
            # callsite serialising it into the prompt. The summary alone
            # is enough for the planner — workers see the full text at
            # execute time.
            tools.append({
                "name": name,
                "service": (t.get("service") or "")[:40],
                "summary": _concise_summary(full_desc),
                "required_params": _required_params(input_schema),
                # Keywords are the strongest non-name ranking signal:
                # the ranker already double-weights them
                # (tool_ranker._tool_search_text). They're pulled from
                # the tool's input_schema.x-keywords so existing rows
                # don't need a migration; an empty list is the safe
                # default for tools authored before keywords landed.
                "keywords": _keywords_from_input_schema(input_schema),
            })

        skills: List[Dict[str, Any]] = []
        for s in raw_skills or []:
            name = (s.get("name") or "").strip()
            if not name:
                continue
            desc = (s.get("description") or "")[:_PER_ITEM_CHAR_CAP]
            if not desc:
                # Skills don't have a top-level "description" column in
                # v1; use the first chars of the content as a hint.
                desc = (s.get("content") or "")[:_PER_ITEM_CHAR_CAP]
            skills.append({
                "name": name,
                "description": desc,
                "category": (s.get("category") or "")[:40],
            })

        # Strip ``docs_kb:`` prefix for orchestrator readability; the
        # runtime re-attaches it when constructing the worker
        # ``knowledge`` dict.
        kb_ids = [r.split(":", 1)[1] if r.startswith("docs_kb:") else r
                  for r in (raw_kbs or [])]

        # Sort everything for deterministic prompts (LLM caches benefit
        # from stable system prompts; this also makes test assertions
        # straightforward).
        tools.sort(key=lambda t: t["name"])
        skills.sort(key=lambda s: s["name"])
        kb_ids = sorted(set(kb_ids))

        return cls(
            tools=tuple(tools),
            skills=tuple(skills),
            kb_ids=tuple(kb_ids),
            built_at=time.time(),
        )

    # ------------------------------------------------------------------
    # Scoping
    # ------------------------------------------------------------------
    def scoped_for_goal(
        self,
        goal: str,
        hints: Optional[Dict[str, Any]] = None,
        *,
        parent_attached_tools: Iterable[str] = (),
        top_k: Optional[int] = None,
        strict_scope: bool = False,
        allowed_extra_domains: Iterable[str] = (),
    ) -> "CapabilityManifest":  # noqa: D401 — see docstring
        # NB: ``parent_attached_tools`` flows through to the ranker (so
        # the parent's tools are always part of the scoped subset) AND is
        # later passed to ``render_for_orchestrator`` so the planner sees
        # them under a dedicated ``[PARENT-ATTACHED TOOLS]`` section. See
        # ``SwarmOrchestrator.plan`` for the two-stage wiring.
        """Return a copy of this manifest with ``tools`` filtered by the ranker.

        Skills and KBs are passed through unchanged — they're already
        small (typically <20 each) so the truncation problem doesn't
        apply. Only ``tools`` (which grows to 100+ in real deployments)
        gets scoped.

        Used by ``SwarmOrchestrator.plan`` on attempt 1 so the LLM sees
        a focused 20-tool manifest instead of the truncated tail of a
        111-entry flat list. If validation still fails, the orchestrator
        retries against the FULL manifest — scoping is an optimisation,
        not a correctness gate.

        ``strict_scope`` (workflow-node mode): when true, the returned
        manifest contains ONLY the parent-attached tools plus the small
        platform-utility set (``code_executor`` when nothing else fits).
        No ranker-picked catalog tools. This is the fix for the "GitLab
        node spawned a JIRA subagent" symptom — with strict scope on,
        the orchestrator physically cannot introduce cross-domain
        capabilities because they aren't in its manifest.
        """
        from .tool_ranker import rank_tools_for_goal, SWARM_MANIFEST_TOP_K

        parent_set = {str(p) for p in (parent_attached_tools or ()) if p}

        if strict_scope and parent_set:
            # Per-node scope with two-tier expansion.
            #
            # Tier 1 — same-domain expansion: catalog tools that share a
            # domain prefix with any attached tool are allowed (e.g.
            # attaches ``gitlab_get_mr`` -> ``gitlab_list_commits`` also
            # visible). Fills gaps in partial attachments.
            #
            # Tier 2 — instruction-declared domains: the caller passes
            # ``allowed_extra_domains`` (derived by the engine from the
            # node's instructions text). When the operator wrote
            # "Perform Jira operations" but attached no jira tool, the
            # engine forwards ``allowed_extra_domains={"jira"}`` here so
            # catalog ``jira_*`` tools enter the manifest. Without this,
            # a mixed-domain instruction with partial attachments would
            # produce plan_validation_failed because the planner cannot
            # cover the un-tooled half of the task.
            #
            # Cross-domain leakage is still blocked: only domains listed
            # in either the attached prefixes OR ``allowed_extra_domains``
            # can enter. Domains the operator did NOT mention anywhere
            # remain filtered out.
            ordered = list(dict.fromkeys(parent_attached_tools))
            attached_prefixes = {
                n.split("_", 1)[0].lower()
                for n in ordered
                if isinstance(n, str) and "_" in n
            }
            extra_prefixes = {
                str(d).strip().lower()
                for d in (allowed_extra_domains or ())
                if d and isinstance(d, str)
            }
            in_scope_prefixes = attached_prefixes | extra_prefixes

            def _tool_in_scope(t: Dict[str, Any]) -> bool:
                name = t.get("name", "")
                if name in parent_set:
                    return True
                if not isinstance(name, str) or "_" not in name:
                    # Un-prefixed catalog tools (e.g. ``code_executor``)
                    # aren't domain-family members; skip so cross-domain
                    # generic tools don't leak in via strict-scope mode.
                    return False
                prefix = name.split("_", 1)[0].lower()
                return prefix in in_scope_prefixes

            in_scope = [t for t in self.tools if _tool_in_scope(t)]
            # Sort: attached tools first (parent order), then neighbours
            # in stable catalog order so the planner sees the operator's
            # curated set at the top and expands only when needed.
            attached_entries = _sort_by_parent_order(
                [t for t in in_scope if t.get("name", "") in parent_set],
                ordered,
            )
            neighbour_entries = [
                t for t in in_scope if t.get("name", "") not in parent_set
            ]
            ranked = attached_entries + neighbour_entries
            if not ranked:
                # Parent-attached tools no longer exist in the catalog
                # (e.g. tool was deleted after the workflow was saved).
                # Refuse to render an empty manifest — fall back to the
                # ranker path so the planner has SOMETHING to work with,
                # and log loudly so the operator can fix the stale
                # attachment.
                logger.warning(f'[AGENT] CapabilityManifest.scoped_for_goal strict_scope=True but NONE of the {len(parent_set)} parent-attached tools exist in the current catalog (missing: {sorted(parent_set)}). Falling back to ranker scope.')
            else:
                logger.info(f'[AGENT] CapabilityManifest.scoped_for_goal strict_scope=True parent_tools={len(parent_set)} attached_kept={len(attached_entries)} same_domain_added={len(neighbour_entries)} (cross-domain tools stripped)')
                # Skip demotion notes — they only make sense against a
                # broader catalog. When the manifest IS the parent's
                # chosen subset, the LLM has no fallback to demote.
                return CapabilityManifest(
                    tools=ranked,
                    skills=self.skills,
                    kb_ids=self.kb_ids,
                    built_at=self.built_at,
                )
            # Fall through to the ranker path when strict scope was
            # requested but all attached tools are stale (empty ranked).

        ranked = rank_tools_for_goal(
            goal=goal,
            hints=hints,
            tools=self.tools,
            parent_attached_tools=parent_attached_tools,
            top_k=top_k if top_k is not None else SWARM_MANIFEST_TOP_K,
        )
        # Goal-conditioned demotion of ``code_executor`` (and other
        # GENERAL-bucket tools) when a specialized tool covers the
        # goal. See ``_apply_demotion_notes`` for the policy. This is
        # the runtime side of A3 — the manifest renderer
        # (``_format_concise_tool_line``) reads the ``avoid_note`` we
        # attach here and shows it inline as part of the tool's entry.
        ranked = _apply_demotion_notes(ranked, goal=goal, hints=hints)
        return CapabilityManifest(
            tools=ranked,
            skills=self.skills,
            kb_ids=self.kb_ids,
            built_at=self.built_at,
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render_for_orchestrator(
        self,
        max_chars: Optional[int] = None,
        *,
        parent_attached_tools: Iterable[str] = (),
    ) -> str:
        """Compact markdown the orchestrator system prompt embeds verbatim.

        Four sections — PARENT-ATTACHED TOOLS (optional), TOOLS, SKILLS,
        KBs — each truncated to fit the overall char budget. We use char
        budgets (not token counts) for the same reason
        ``kb_retriever._format_context`` does: no tiktoken dep, and the
        LLM has plenty of slack at this scale.

        ``parent_attached_tools`` is the list of tool NAMES the parent
        agent has explicitly attached (e.g. the user picked
        ``gitlab_list_commits`` + ``gitlab_get_merge_request`` +
        ``gitlab_list_repository_tree`` on the Agents Tab). When
        non-empty we render those tools FIRST under a dedicated header
        so the planner LLM knows the user has already decided which
        tools the swarm should distribute across workers. Without this
        signal the planner sees them mixed in with the full catalog and
        sometimes substitutes ``code_executor`` or ignores half of them.
        """
        budget = int(max_chars or _MAX_MANIFEST_CHARS)
        out_parts: List[str] = []

        # PARENT-ATTACHED TOOLS — surfaced FIRST so the planner reads
        # them before the broader catalog. We deliberately echo the same
        # tool entries (full description + required params) here even
        # though they ALSO appear in the [TOOLS] section below — the
        # duplication is intentional, it gives the planner an explicit
        # "user-curated short list" while the [TOOLS] section remains
        # the formal grounding catalog used by ``validate_plan``. The
        # extra ~200 chars per parent tool is well within the budget.
        parent_set = {str(p) for p in (parent_attached_tools or ()) if p}
        if parent_set:
            parent_entries = [t for t in self.tools if t.get("name") in parent_set]
            if parent_entries:
                # Preserve the order the parent attached them in.
                parent_entries = _sort_by_parent_order(parent_entries, parent_attached_tools)
                lines = [_format_concise_tool_line(t) for t in parent_entries]
                lines = [ln for ln in lines if ln]  # drop empties
                out_parts.append(
                    "### PARENT-ATTACHED TOOLS\n"
                    "(The parent agent has these tools wired up by the user. "
                    "Workers MUST prefer these for the work they describe; "
                    "distribute them across workers when the goal has multiple "
                    "independent sub-tasks.)\n"
                    + "\n".join(lines)
                )

        # TOOLS — grouped by service prefix so the orchestrator LLM can
        # see semantic clusters (gitlab_*, jira_*, postgres_*, …) and
        # pick a purpose-built tool from the right family before falling
        # back to ``code_executor``. A flat 200-entry list pushes the
        # LLM toward the universal escape hatch because it can't easily
        # scan for the right service family. Grouping keeps the same
        # information density but adds the family signal.
        if self.tools:
            out_parts.append("### TOOLS\n" + _render_tools_grouped(self.tools))
        else:
            out_parts.append("### TOOLS\n  (no tools currently published)")

        # SKILLS
        if self.skills:
            skill_lines = [f"  - {s['name']}: {s['description']}" if s['description']
                           else f"  - {s['name']}"
                           for s in self.skills]
            out_parts.append("### SKILLS\n" + "\n".join(skill_lines))
        else:
            out_parts.append("### SKILLS\n  (no skills currently published)")

        # KBs
        if self.kb_ids:
            kb_lines = [f"  - {kid}" for kid in self.kb_ids]
            out_parts.append(
                "### KNOWLEDGE BASES (use as knowledge={\"mode\":\"existing_kb\",\"kb_id\":\"<id>\"})\n"
                + "\n".join(kb_lines)
            )
        else:
            out_parts.append("### KNOWLEDGE BASES\n  (no KBs currently available)")

        rendered = "\n\n".join(out_parts)
        if len(rendered) > budget:
            # Truncate from the end with a clear marker — better to drop
            # the tail of the skills list than to silently feed the LLM a
            # half-cut tool line.
            rendered = rendered[: budget - 80].rstrip() + "\n  ... (manifest truncated)"
        return rendered

    # ------------------------------------------------------------------
    # Plan validation
    # ------------------------------------------------------------------
    def validate_plan(self, plan: Any) -> List[str]:
        """Return a list of human-readable validation errors.

        Empty list = plan is valid (every tool/skill/KB it references
        exists in this manifest).
        """
        from .types import SwarmPlan

        errs: List[str] = []
        if not isinstance(plan, SwarmPlan):
            errs.append(f"plan must be a SwarmPlan, got {type(plan).__name__}")
            return errs

        known_tools = self.tool_names
        known_skills = self.skill_names
        known_kbs = self.kb_id_set

        for w in plan.workers:
            bad_tools = [t for t in w.tools if t not in known_tools]
            if bad_tools:
                errs.append(_format_unknown(
                    w.role_id, "tool", bad_tools, known_tools, "TOOLS",
                ))
            bad_skills = [s for s in w.skills if s not in known_skills]
            if bad_skills:
                errs.append(_format_unknown(
                    w.role_id, "skill", bad_skills, known_skills, "SKILLS",
                ))
            mode = (w.knowledge or {}).get("mode", "none")
            if mode == "existing_kb":
                kb_id = (w.knowledge or {}).get("kb_id", "")
                if kb_id and kb_id not in known_kbs:
                    errs.append(
                        f"worker '{w.role_id}': unknown kb_id {kb_id!r}. "
                        f"Pick from the [CAPABILITY MANIFEST] KNOWLEDGE BASES section only."
                    )
            elif mode not in ("none", "existing_kb"):
                errs.append(
                    f"worker '{w.role_id}': knowledge.mode must be 'none' or "
                    f"'existing_kb', got {mode!r}"
                )
        return errs


# Tools that always belong in the rendered GENERAL bucket regardless
# of how the catalog tagged them — they're cross-cutting / last-resort,
# not service-bound. Drift-prone if redefined; keep in sync with
# ``app/swarm/_shared.py`` if it ever centralises this constant.
_GENERAL_TOOL_NAMES = frozenset({
    "code_executor", "web_fetch", "web_search", "spawn_swarm",
    "delegate_to_workflow", "delegate_to_agent", "ask_human",
    "read_skill_file",
})


# Goal-conditioned demotion (A3). Each entry maps a GENERAL-bucket tool
# to the keyword universe it covers — when the user's goal mentions any
# of those words AND the scoped manifest contains a non-general tool
# that ALSO covers them, the GENERAL tool gets an ``avoid_note`` telling
# the planner to prefer the specialized choice. The dump
# ``20260620T090542_096282_produce-a-professional-variance_attempt1``
# is the regression this targets: the planner picked
# ``code_executor`` (well, ``execute_command``, which we alias-repair to
# ``code_executor``) for a variance-report goal even though
# a specialized variance-report tool was visible. After A3 the renderer
# shows the planner an explicit "PREFER ..." note next to
# ``code_executor`` whenever a specialized fit exists.
#
# Keep the trigger sets small and unambiguous — these are the tokens
# that, when present in the goal, mean "domain-specific tool fits". Add
# entries only when you have a real dump showing the wrong pick.
_GENERAL_TOOL_DEMOTION_TRIGGERS: Dict[str, frozenset] = {
    "code_executor": frozenset({
        # Document / report generation
        "docx", "doc", "word", "report", "pdf", "spreadsheet", "excel",
        "workbook", "xlsx", "csv",
        # Finance / data analysis
        "variance", "budget", "actual", "actuals", "reconcile",
        "reconciliation", "finance", "financial", "ledger",
        # Charts / tables
        "chart", "graph", "table", "plot",
    }),
    "web_search": frozenset({
        # Service-specific lookups
        "gitlab", "github", "jira", "confluence", "merge", "pull",
        "commit", "issue", "ticket", "mr", "pr",
    }),
    "web_fetch": frozenset({
        "gitlab", "github", "jira", "confluence",
    }),
}


def _apply_demotion_notes(
    tools: Tuple[Dict[str, Any], ...],
    *,
    goal: str,
    hints: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], ...]:
    """Attach ``avoid_note`` to GENERAL-bucket tools when a specialized
    tool in the scoped set covers the same goal.

    Pure function. Returns a NEW tuple of NEW dicts — never mutates the
    input. That keeps the cached full manifest immutable while letting
    the per-call scoped projection carry the demotion.

    Decision per general tool:
      1. Lookup its trigger keyword set in ``_GENERAL_TOOL_DEMOTION_TRIGGERS``.
      2. Tokenise the goal (+ json-rendered hints) the same way the
         ranker does — lowercase, alnum-only, length ≥ 3. Reusing the
         ranker's tokeniser keeps "what the planner saw" and "what we
         demote against" in lockstep.
      3. Compute the intersection of trigger keywords with goal tokens.
         No hit → no note (the goal doesn't actually need a specialized
         alternative).
      4. Find the first non-general tool in the SCOPED set whose own
         keywords intersect the same trigger words. That's the
         "specialized fit" we point the planner at.
      5. Attach an inline ``avoid_note`` naming the specialized tool.

    Conservative on purpose:
    * No fuzzy matching. If the goal says ``budget`` and the trigger
      set has ``budget``, we fire. Subword matches would chase noise.
    * One specialized tool named per demotion, even if several fit —
      the planner reads the first one and goes; surfacing five
      candidates dilutes the hint and burns tokens.
    * GENERAL tools without an entry in the trigger map are left
      alone. ``spawn_swarm`` and ``delegate_to_*`` never get demoted —
      they're not substitutes for domain tools.
    """
    if not tools:
        return tools

    # Tokenise the goal once. Reuses tool_ranker's algorithm so the
    # token universe matches what scoring saw.
    from .tool_ranker import _tokenise as _ranker_tokenise
    text = goal or ""
    if hints:
        try:
            import json as _json
            text += " " + _json.dumps(hints, default=str, ensure_ascii=False)
        except Exception:
            text += " " + repr(hints)
    goal_tokens = _ranker_tokenise(text)
    if not goal_tokens:
        return tools

    # Build the specialized-tool lookup: per trigger keyword, the
    # non-general tools (already in the scoped set) whose own keywords
    # cover it. We iterate the scoped tools once.
    specialists_by_kw: Dict[str, List[str]] = {}
    for t in tools:
        name = t.get("name") or ""
        if not name or name in _GENERAL_TOOL_NAMES:
            continue
        kws = t.get("keywords") or []
        if not isinstance(kws, (list, tuple)):
            continue
        for kw in kws:
            if not isinstance(kw, str) or not kw:
                continue
            specialists_by_kw.setdefault(kw.lower(), []).append(name)

    out: List[Dict[str, Any]] = []
    for t in tools:
        name = t.get("name") or ""
        trigger_kws = _GENERAL_TOOL_DEMOTION_TRIGGERS.get(name)
        if not trigger_kws or not name:
            out.append(t)
            continue
        # Trigger words this goal hit.
        hit = trigger_kws & goal_tokens
        if not hit:
            out.append(t)
            continue
        # Find the first specialized tool keyed by any hit word.
        # Sort hit words for determinism, then sort the candidates by
        # name so test assertions are stable.
        preferred: Optional[str] = None
        for kw in sorted(hit):
            cands = specialists_by_kw.get(kw)
            if cands:
                preferred = sorted(cands)[0]
                break
        if preferred is None:
            out.append(t)
            continue
        new_t = dict(t)
        new_t["avoid_note"] = (
            f"PREFER {preferred} for this goal — it's the specialized "
            f"fit. Use {name} only as a fallback."
        )
        out.append(new_t)
    return tuple(out)

# Service-tag values from the catalog that don't actually denote a
# service family and should fall back to name-prefix derivation.
_NON_SERVICE_TAGS = frozenset({"platform", "mcp"})

_GENERAL_GROUP_KEY = "general"


def _split_prefix(name: str) -> str:
    """Return the service-prefix segment of ``name`` (``"" `` when none)."""
    return name.split("_", 1)[0] if "_" in name else ""


# Char cap for the orchestrator's one-line summary. Existing canonical
# tool descriptions start with a clean imperative sentence ("List recent
# commits on a branch in a GitLab repository"), typically 60-120 chars —
# we take the first sentence, capped, so this matches the natural shape
# of the data without re-authoring anything.
_CONCISE_SUMMARY_CHARS = int(os.getenv("SWARM_CONCISE_SUMMARY_CHARS", "120"))


def _concise_summary(description: str) -> str:
    """Pull a one-line imperative summary from the tool's full description.

    SWARM-SCOPED helper. We do NOT modify the canonical
    ``tools_catalog.description`` column — this is an in-memory
    projection used only by the swarm orchestrator's manifest renderer.

    Strategy: take text up to the first sentence terminator
    (``\\n`` / ``. `` / ``? `` / ``! ``), cap to
    ``_CONCISE_SUMMARY_CHARS``. Deterministic, dependency-free, zero LLM
    cost. Produces clean summaries for the existing canonical tools
    ("List recent commits on a branch in a GitLab repository. Returns
    each commit's …" → "List recent commits on a branch in a GitLab
    repository").
    """
    if not description:
        return ""
    text = description.strip()
    cut = len(text)
    for sep in ("\n", ". ", "? ", "! "):
        i = text.find(sep)
        if i != -1 and i < cut:
            cut = i
    first = text[:cut].strip() or text
    if len(first) > _CONCISE_SUMMARY_CHARS:
        first = first[: _CONCISE_SUMMARY_CHARS - 1].rstrip() + "…"
    return first


def _keywords_from_input_schema(input_schema: Any) -> List[str]:
    """Pull goal-matching keywords out of the tool's ``input_schema``.

    We piggy-back keywords on the existing ``input_schema`` JSON column
    rather than migrating ``tools_catalog`` for a new array column —
    backward-compatible with every existing row (those simply produce
    an empty list).

    Recognised shapes (first non-empty wins):
      * ``input_schema["x-keywords"]``   — preferred. The ``x-`` prefix
        matches JSON-Schema's convention for vendor extensions, so
        existing JSON-Schema validators ignore it cleanly.
      * ``input_schema["keywords"]``     — fallback for tools written
        before we settled on the ``x-`` convention.

    Both must be ``List[str]``; anything else is treated as empty so a
    malformed row never crashes the manifest build. Capped at 12
    entries: more than that just bloats the ranker's overlap denominator
    and dilutes the signal we want from the genuinely-relevant words.

    The ranker (``tool_ranker._tool_search_text``) already reads a
    ``keywords`` field on the projected tool dict and double-weights it —
    this helper exists to populate that field from the canonical row.
    """
    if not isinstance(input_schema, dict):
        return []
    raw = input_schema.get("x-keywords")
    if raw is None:
        raw = input_schema.get("keywords")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for kw in raw:
        if not isinstance(kw, str):
            continue
        kw = kw.strip().lower()
        if not kw:
            continue
        if kw in out:
            continue
        out.append(kw)
        if len(out) >= 12:
            break
    return out


def _required_params(input_schema: Any) -> List[str]:
    """Extract the list of REQUIRED parameter names from an input_schema.

    SWARM-SCOPED. Surfaced in the orchestrator's concise manifest so the
    planner LLM can write self-contained ``worker.task`` strings that
    actually include the parameters each tool needs (project_id,
    mr_iid, etc.). Optional parameters are deliberately omitted to keep
    the manifest dense — the worker hydrates the full ``input_schema``
    via Fix #1 and sees every parameter at invocation time.

    Returns at most 8 names so the rendered line never balloons.
    """
    if not isinstance(input_schema, dict):
        return []
    required = input_schema.get("required") or []
    if not isinstance(required, list):
        return []
    out: List[str] = []
    for name in required:
        if isinstance(name, str) and name:
            out.append(name)
            if len(out) >= 8:
                break
    return out


def _format_concise_tool_line(t: Dict[str, Any]) -> str:
    """Render one tool as a single-line orchestrator manifest entry.

    Shape:
        ``  - <name> (required: <p1>, <p2>): <summary> [kw: a, b, c]<avoid_note>``

    Each segment is optional and falls back gracefully:
    * no ``summary`` → use truncated ``description`` (back-compat for
      direct constructors that bypass ``_build_uncached``)
    * no ``required_params`` → drop the parentheses entirely
    * no ``keywords`` → omit the ``[kw: ...]`` segment
    * no ``avoid_note`` → no goal-conditioned demotion suffix
    * no description at all → just ``  - <name>``

    The ``[kw: ...]`` suffix is short (≤ 6 entries) on purpose: keywords
    exist to influence the planner's choice, not to act as a second
    description. The ranker already sees the full list — the planner
    benefits more from a tight readable hint.

    The ``avoid_note`` suffix is set by ``scoped_for_goal`` when a
    specialized tool covers the goal — see A3 below.
    """
    name = t.get("name") or ""
    if not name:
        return ""
    summary = (t.get("summary") or t.get("description") or "").strip()
    req = t.get("required_params") or []
    req_hint = f" (required: {', '.join(req)})" if req else ""
    kws = t.get("keywords") or []
    kw_hint = ""
    if isinstance(kws, (list, tuple)) and kws:
        kw_hint = f" [kw: {', '.join(str(k) for k in kws[:6])}]"
    avoid_note = (t.get("avoid_note") or "").strip()
    avoid_hint = f" — {avoid_note}" if avoid_note else ""
    if summary:
        return f"  - {name}{req_hint}: {summary}{kw_hint}{avoid_hint}"
    if kw_hint or avoid_hint:
        return f"  - {name}{req_hint}{kw_hint}{avoid_hint}"
    return f"  - {name}{req_hint}"


def _render_tools_grouped(tools: List[Dict[str, Any]]) -> str:
    """Group manifest tool rows by service prefix and render as text.

    Service is derived from the explicit ``service`` field if present,
    otherwise from the name prefix (``gitlab_get_mr`` → ``gitlab``).
    Output is deterministic — known services alphabetical, then GENERAL
    last so the LLM consistently sees cross-cutting / last-resort tools
    as the fallback bucket.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in tools:
        name = t.get("name") or ""
        if not name:
            continue
        if name in _GENERAL_TOOL_NAMES:
            svc = _GENERAL_GROUP_KEY
        else:
            svc = (t.get("service") or "").strip().lower()
            if not svc or svc in _NON_SERVICE_TAGS:
                svc = _split_prefix(name) or _GENERAL_GROUP_KEY
        groups.setdefault(svc, []).append(t)

    ordered = sorted(s for s in groups if s != _GENERAL_GROUP_KEY)
    if _GENERAL_GROUP_KEY in groups:
        ordered.append(_GENERAL_GROUP_KEY)

    blocks: List[str] = []
    for svc in ordered:
        header = f"#### {svc.upper()}"
        if svc == _GENERAL_GROUP_KEY:
            header += "  (last-resort / cross-cutting)"
        # Concise per-line shape:
        #   - <name> (required: a, b): <one-line summary>
        # Falls back gracefully when summary/required_params aren't
        # populated (defends against direct callers that build manifest
        # tool dicts outside ``_build_uncached``).
        lines = [
            _format_concise_tool_line(t)
            for t in groups[svc]
        ]
        blocks.append(header + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _format_unknown(
    role_id: str, kind: str, bad: List[str], known: set, section: str,
) -> str:
    """Build a ``unknown <kind>(s)`` validation error with a did_you_mean hint."""
    suggestions = _suggest_replacements(bad, known)
    hint = f" did_you_mean: {suggestions}" if suggestions else ""
    return (
        f"worker '{role_id}': unknown {kind}(s) {bad!r}. "
        f"Pick from the [CAPABILITY MANIFEST] {section} section only.{hint}"
    )


def _suggest_replacements(bad: List[str], known: set, *, top_n: int = 3) -> List[str]:
    """Return up to ``top_n`` closest manifest entries for the bad names.

    Surfaced to the orchestrator LLM as a ``did_you_mean`` hint on the
    retry round so it self-corrects to a verbatim manifest entry. The
    server never rewrites the plan — silent rewrites occasionally swap
    in the wrong tool because lexical similarity does not imply
    semantic equivalence.

    Matching is family-scoped: ``gitlab_*`` candidates aren't suggested
    for ``jira_*`` requests, because cross-family suggestions are
    almost always wrong. When no prefix is present we use the global
    pool so blank-prefix names (``code_executor``, ``web_search``) still
    get useful hints.
    """
    if not bad or not known:
        return []
    # Cluster bad names by prefix so the same-family pool is built once
    # per prefix, not once per bad name (the LLM tends to make the same
    # family mistake multiple times in one plan).
    by_prefix: Dict[str, List[str]] = {}
    for name in bad:
        if not isinstance(name, str) or not name:
            continue
        by_prefix.setdefault(_split_prefix(name), []).append(name)
    if not by_prefix:
        return []

    known_list = sorted(known)
    # Index ``known`` by prefix once so the per-prefix pool lookup is O(1).
    prefix_index: Dict[str, List[str]] = {}
    for k in known_list:
        prefix_index.setdefault(_split_prefix(k), []).append(k)

    out: List[str] = []
    seen: set = set()
    for src_prefix, names in by_prefix.items():
        pool = prefix_index.get(src_prefix) if src_prefix else known_list
        if not pool:
            pool = known_list
        for name in names:
            # cutoff=0.6 is the difflib default — catches plural / verb-
            # swap variants (``gitlab_get_merge_request`` →
            # ``gitlab_get_mr``) without surfacing unrelated noise.
            for cand in difflib.get_close_matches(name, pool, n=top_n, cutoff=0.6):
                if cand not in seen:
                    seen.add(cand)
                    out.append(cand)
    return out[:top_n]


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _evict_expired_locked(now: float) -> None:
    """Drop entries whose expiry is in the past.

    Must be called with ``_cache_lock`` held (caller responsibility).
    """
    stale = [k for k, (exp, _) in _cache.items() if exp <= now]
    for k in stale:
        _cache.pop(k, None)


def _reset_cache_for_tests() -> None:
    """Drop the manifest cache. Test-only."""
    _cache.clear()


__all__ = ["CapabilityManifest"]
