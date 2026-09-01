# SPDX-License-Identifier: Apache-2.0
"""Goal-conditioned tool ranker for the swarm orchestrator.

With a 111-tool catalog, dumping every tool into the orchestrator's
system prompt is both expensive (~13K chars / ~3K tokens) and
counter-productive — past ~30 entries the LLM's "lost in the middle"
effect kicks in and tool-picking accuracy drops sharply. The
``render_for_orchestrator`` budget then silently truncates the tail
of the manifest, hiding entire service families from the planner.

This module reduces the catalog to a small (~20) candidate set that's
*relevant to the user's goal*. Algorithm is deterministic, dependency-
free, and O(N) in the tool count:

1. **Always include** the GENERAL bucket (``code_executor``,
   ``web_search``, ``spawn_swarm``, …) — the planner always needs the
   cross-cutting / last-resort fallbacks visible.
2. **Always include** the parent's already-attached tools — the
   orchestrator must be able to see them to honour the "parent handles
   this directly" branch (and not spawn a redundant worker).
3. **Service-family expansion** — when the goal mentions a known
   service prefix (``gitlab``, ``jira``, ``postgres``, …) we include
   every tool in that family. The N-tools-per-service is small (3-15)
   and a goal that mentions GitLab almost always needs more than one
   GitLab tool, so including the whole family is cheaper than picking.
4. **Token-overlap ranking** for the remainder — score each remaining
   tool by token-overlap of (goal + hints) against
   (name + description + keywords). Top-K survives.

The scoped manifest still goes through the same validation that the
full manifest would; the orchestrator's existing ``did_you_mean``
hint surfaces near-miss candidates on retry. If validation fails on
the scoped manifest, ``SwarmOrchestrator.plan`` widens to the full
manifest for retry — the scoping is a speed/cost optimisation, not a
correctness gate.

Tuning knobs (env):
    SWARM_MANIFEST_TOP_K           default 20
    SWARM_MANIFEST_GOAL_TOKEN_MIN  default 3 (min char length per token)
"""
from __future__ import annotations


import os
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from core.logger import logger
# Default 30 is the LLM-accuracy threshold cited in the module docstring:
# beyond ~30 tool entries the "lost in the middle" effect degrades
# tool-pick accuracy sharply. With multi-family goals (Jira+GitLab,
# multiple domain tool families) the per-family budget gets tight at 20
# (we observed legitimate scenarios needing 6-7 family-specific tools
# AND general-bucket survival AND parent-attached), so 30 is a saner
# default that still keeps the prompt under the accuracy cliff.
# Operators on bandwidth-constrained models can drop this back to 20.
SWARM_MANIFEST_TOP_K = int(os.getenv("SWARM_MANIFEST_TOP_K", "30"))
_TOKEN_MIN_LEN = int(os.getenv("SWARM_MANIFEST_GOAL_TOKEN_MIN", "3"))


# Cross-cutting / last-resort tools that are always visible to the
# orchestrator regardless of the goal text. Kept in sync with
# ``capability_manifest._GENERAL_TOOL_NAMES`` — same semantic meaning.
_ALWAYS_INCLUDE: frozenset = frozenset({
    "spawn_swarm",
    "delegate_to_workflow", "delegate_to_agent", "ask_human",
    "read_skill_file","code_executor", "web_fetch", "web_search" 
})


def _tokenise(text: str) -> Set[str]:
    """Lowercase + split on non-alnum; drop short tokens.

    Length floor strips ``a``, ``of``, ``to``, ``in`` etc. without us
    maintaining a stopword list — overlap scoring catches domain words
    like ``merge``, ``request``, ``commit``, ``file``.
    """
    if not text:
        return set()
    out: Set[str] = set()
    cur: List[str] = []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
            continue
        if cur:
            tok = "".join(cur)
            if len(tok) >= _TOKEN_MIN_LEN:
                out.add(tok)
            cur = []
    if cur:
        tok = "".join(cur)
        if len(tok) >= _TOKEN_MIN_LEN:
            out.add(tok)
    return out


def _tool_prefix(name: str) -> str:
    return name.split("_", 1)[0] if "_" in name else ""


def _tool_search_text(tool: Dict[str, Any]) -> str:
    """Concatenate searchable fields. Keywords (when present) get extra
    weight by repetition — token overlap counts them twice.

    Prefers ``description`` when present (richer ranking signal) but
    falls back to ``summary`` for callers that work off the slimmed
    swarm manifest projection — ``CapabilityManifest._build_uncached``
    no longer persists the full description on tool dicts, so the
    ranker reads the concise summary instead. Both fields are token-
    boundary text; the ranker doesn't care which it sees.
    """
    parts = [
        tool.get("name") or "",
        tool.get("description") or tool.get("summary") or "",
    ]
    kws = tool.get("keywords") or []
    if isinstance(kws, (list, tuple)):
        kw_text = " ".join(str(k) for k in kws if k)
        parts.append(kw_text)
        parts.append(kw_text)  # double weight
    return " ".join(parts)


def _service_prefixes_in_goal(
    goal_tokens: Set[str], known_prefixes: Iterable[str],
) -> Set[str]:
    """Return the set of known service prefixes that the goal mentions.

    Match is case-insensitive and exact-token (already lowered by
    ``_tokenise``). We don't substring-match because that produces
    false positives — e.g. goal "list jiraffe parks" should not match
    the ``jira`` family.
    """
    return {p for p in known_prefixes if p and p in goal_tokens}


def rank_tools_for_goal(
    *,
    goal: str,
    hints: Optional[Dict[str, Any]],
    tools: Tuple[Dict[str, Any], ...],
    parent_attached_tools: Iterable[str] = (),
    top_k: int = SWARM_MANIFEST_TOP_K,
) -> Tuple[Dict[str, Any], ...]:
    """Return a scoped subset of ``tools`` ranked for ``goal``.

    Inclusion priority (a tool kept by any rule is in the result):

    1. Name is in ``_ALWAYS_INCLUDE`` (cross-cutting / last-resort).
    2. Name is in ``parent_attached_tools`` (orchestrator needs to see
       the parent's toolset to decide skip-vs-spawn).
    3. Service prefix is mentioned verbatim in goal+hints (entire
       family included).
    4. Token-overlap score against goal+hints fills the remaining
       budget up to ``top_k`` tools.

    Ordering: priority 1 (general bucket) appears LAST in the rendered
    output so the LLM consistently sees them as fallback; the helper
    method ``CapabilityManifest._render_tools_grouped`` handles that
    ordering. Here we only decide membership. Use code executor tool 
    only when no specialized tool covers the requested task.
    """
    if not tools:
        return ()
    # Cap top_k to a sane minimum so callers passing 0 / negative
    # don't accidentally produce an empty manifest.
    top_k = max(int(top_k), 5)

    # Build the goal/hint token set once.
    goal_text = goal or ""
    if hints:
        try:
            import json as _json
            goal_text += " " + _json.dumps(hints, default=str, ensure_ascii=False)
        except Exception:
            goal_text += " " + repr(hints)
    goal_tokens = _tokenise(goal_text)

    # Index by prefix once — also the set of "known service prefixes"
    # we test against the goal for family expansion.
    prefix_index: Dict[str, List[Dict[str, Any]]] = {}
    for t in tools:
        name = t.get("name") or ""
        if not name:
            continue
        prefix_index.setdefault(_tool_prefix(name), []).append(t)

    parent_set: Set[str] = {str(p) for p in (parent_attached_tools or ()) if p}
    matched_prefixes = _service_prefixes_in_goal(goal_tokens, prefix_index.keys())

    # If the goal text alone doesn't name a service family but the
    # parent's attached tools do (e.g. parent has ``jira_list_issues``
    # for a goal "track worklog entries this week"), expand that
    # family too. The parent's toolset is the strongest signal of
    # which service this swarm is about — without it, goals phrased
    # in domain language (worklog, ticket, MR, pipeline) get drowned
    # by random matches from the wrong service family.
    parent_prefixes = {
        _tool_prefix(p) for p in parent_set if _tool_prefix(p)
    }
    matched_prefixes |= (parent_prefixes & set(prefix_index.keys()))

    # Tier priorities — lower = keep first when over budget.
    # Rule 1 (general bucket) is the most important pin: it's the
    # last-resort fallback, removing it would leave goals tool-less.
    # Rule 2 (parent-attached) is next: dropping a parent tool defeats
    # the "parent handles this directly" branch in the orchestrator.
    # Rule 4 (overlap-ranked) is third: these are tools whose own
    # keywords genuinely match the goal. Rule 3 (family expansion) is
    # LAST because that's where the bloat comes from — a multi-family
    # goal can drag in 80+ tools per service, drowning the rule-4
    # signal. When the cap fires, family-expanded tools that don't ALSO
    # overlap goal tokens are what gets cut first.
    _TIER_RULE1_GENERAL = 0
    _TIER_RULE2_PARENT  = 1
    _TIER_RULE4_OVERLAP = 2
    _TIER_RULE3_FAMILY  = 3

    selected: Dict[str, Tuple[int, Dict[str, Any]]] = {}

    def _add(t: Dict[str, Any], tier: int) -> None:
        name = t.get("name")
        if not name:
            return
        # Keep the LOWEST tier we've seen for a tool — a tool that
        # qualifies under both rule 1 and rule 3 should be pinned with
        # rule 1's priority.
        prev = selected.get(name)
        if prev is None or tier < prev[0]:
            selected[name] = (tier, t)

    # Rule 1: always-include general bucket.
    for t in tools:
        if t.get("name") in _ALWAYS_INCLUDE:
            _add(t, _TIER_RULE1_GENERAL)

    # Rule 2: parent's already-attached tools.
    for t in tools:
        if t.get("name") in parent_set:
            _add(t, _TIER_RULE2_PARENT)

    # Rule 3: service-family expansion.
    for pfx in matched_prefixes:
        for t in prefix_index.get(pfx, ()):
            _add(t, _TIER_RULE3_FAMILY)

    # Rule 4: top-K by token overlap for the rest.
    if len(selected) < top_k and goal_tokens:
        scored: List[Tuple[int, str, Dict[str, Any]]] = []
        for t in tools:
            name = t.get("name")
            if not name or name in selected:
                continue
            cand_tokens = _tokenise(_tool_search_text(t))
            overlap = len(goal_tokens & cand_tokens)
            if overlap > 0:
                # Name used as tiebreaker for determinism.
                scored.append((overlap, name, t))
        scored.sort(key=lambda r: (-r[0], r[1]))
        remaining = top_k - len(selected)
        for _score, _name, t in scored[:remaining]:
            _add(t, _TIER_RULE4_OVERLAP)

    # ── Hard ceiling: trim to top_k with explicit tier priority ──
    # Rules 1-3 do not respect ``top_k`` on their own — the family
    # expansion at rule 3 can blow past the ceiling by dozens of tools
    # on multi-family goals (the dump
    # ``20260620T152620_253754_fetch-two-pieces-of-information_attempt1``
    # showed 114 tools for a Jira+GitLab goal, ~6× the documented
    # SWARM_MANIFEST_TOP_K=20). A 100+ tool manifest is exactly the
    # regime where the LLM's "lost in the middle" effect kicks in and
    # tool-pick accuracy collapses.
    #
    # Trim within tier by:
    #   1. tier ascending (rule 1 first)
    #   2. NAME-token overlap with goal tokens DESCENDING. The tool's
    #      own name is a far stronger intent signal than its description
    #      — ``jira_add_comment`` for a goal saying "comment" should beat
    #      ``jira_update_issue`` even if the latter's description
    #      mentions more goal words. Without this nuance the trim
    #      systematically favours tools with verbose descriptions over
    #      tools whose action verb is in the name itself.
    #   3. full-text overlap DESCENDING (name + summary + keywords) as
    #      the secondary tiebreak — same metric the rule-4 ranker uses,
    #      so it stays consistent across selection vs trim.
    #   4. name ascending (deterministic final tiebreak)
    if len(selected) > top_k and goal_tokens:
        def _trim_sort_key(item):
            name, (tier, tool) = item
            name_tokens = _tokenise(name)
            name_overlap = len(goal_tokens & name_tokens)
            full_overlap = len(goal_tokens & _tokenise(_tool_search_text(tool)))
            return (tier, -name_overlap, -full_overlap, name)
        keep = sorted(selected.items(), key=_trim_sort_key)[:top_k]
        selected = dict(keep)
    elif len(selected) > top_k:
        # No goal tokens to score against — fall back to tier + name.
        def _trim_sort_key_no_goal(item):
            name, (tier, _tool) = item
            return (tier, name)
        keep = sorted(selected.items(), key=_trim_sort_key_no_goal)[:top_k]
        selected = dict(keep)

    out = tuple(t for _tier, t in selected.values())
    # Per-tier counts in the final output. More honest than the prior
    # math which assumed no trimming and silently went negative on
    # multi-family goals once the top_k cap fires.
    tier_counts = {
        _TIER_RULE1_GENERAL: 0,
        _TIER_RULE2_PARENT:  0,
        _TIER_RULE3_FAMILY:  0,
        _TIER_RULE4_OVERLAP: 0,
    }
    for tier, _t in selected.values():
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
    logger.debug(f'[AGENT] tool_ranker: {len(out)} of {len(tools)} tools selected (general={tier_counts[_TIER_RULE1_GENERAL]}, parent={tier_counts[_TIER_RULE2_PARENT]}, families={sorted(matched_prefixes)}, family_kept={tier_counts[_TIER_RULE3_FAMILY]}, by_overlap={tier_counts[_TIER_RULE4_OVERLAP]}, top_k={top_k})')
    return out


__all__ = ["rank_tools_for_goal", "SWARM_MANIFEST_TOP_K"]
