# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SECTION QUERY ROUTER — bias retrieval toward named sections
#
# Problem this solves:
#   When a user asks "give me the Prerequisites from SettleNXT release notes",
#   pure semantic similarity often retrieves a mix of chunks from several
#   sections (Prerequisites + Installation Procedure + Checklist), and the
#   LLM blends them. Even with a strict prompt the model can drift because
#   the wrong content is in its context window in the first place.
#
#   The structured chunker (store.docs_store._chunk_document_structured)
#   already attaches section_name and section_path to every embedded chunk,
#   and the hybrid search layer already surfaces those columns in its
#   results. We just need to (1) detect the section the user is asking about
#   and (2) re-rank results so chunks from that section bubble to the top.
#
# Design choices:
#   - Pure re-ranker, no DB schema change. We accept a list of hits and
#     return a re-ordered list. Callers can opt in per query.
#   - Vocabulary-driven detection — uses the same canonical list as
#     core.section_promoter so a promoted section is also detectable.
#   - Conservative: a section match only adds a bonus; it never drops
#     non-matching chunks entirely. If detection misfires, the worst case
#     is no improvement, not a degraded answer.
#   - Multiple matches are supported: "Prerequisites and Checklist" boosts
#     chunks from either section.
# ============================================================

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from core.logger import logger


# All canonical section names known to the promoter — kept in one place
# so detection and promotion stay in sync. Importing on demand avoids a
# circular import at module load.
def _load_vocabulary() -> Dict[str, List[str]]:
    """
    Return {doc_kind: [canonical section names]}.

    Stays in lockstep with core.section_promoter. We invert the promoter's
    private vocabulary structure into a flat lookup for query-time matching.
    """
    try:
        from core import section_promoter as _sp
        return {
            kind: _sp.vocabulary_for(kind)
            for kind in _sp.known_doc_kinds()
        }
    except Exception:
        return {}


# A flat set of all canonical sections across all doc kinds — used when the
# query doesn't pin a doc kind. This is small (dozens), so a linear scan is
# cheap.
def _all_known_sections() -> List[str]:
    vocab = _load_vocabulary()
    seen: dict = {}
    out: List[str] = []
    for kind, sections in vocab.items():
        for s in sections:
            key = s.lower()
            if key in seen:
                continue
            seen[key] = True
            out.append(s)
    return out


# ----------------------------------------------------------------------
# Section detection in the query
# ----------------------------------------------------------------------

# Word-boundary regex helper. We compile per call because the vocabulary
# is small and the function is invoked per query — caching here would
# add complexity without a meaningful win.

def _build_detect_pattern(section_names: List[str]) -> Optional[re.Pattern]:
    if not section_names:
        return None
    # Sort longest first so "Release Details" matches before "Release".
    parts = [re.escape(s) for s in sorted(section_names, key=len, reverse=True)]
    return re.compile(
        r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def detect_sections_in_query(
        query: str,
        doc_kind: Optional[str] = None,
) -> List[str]:
    """
    Return the canonical section names mentioned in the query. Case-insensitive,
    whole-token match. Empty list when nothing matches.

    Args:
        query:    The raw user question.
        doc_kind: When provided, restricts detection to that kind's vocabulary
                  (e.g. "RELEASE_NOTE"). When None, all known sections are
                  considered.
    """
    if not query:
        return []

    vocab = _load_vocabulary()
    if doc_kind:
        candidates = vocab.get(doc_kind.upper().strip()) or []
    else:
        candidates = _all_known_sections()

    pattern = _build_detect_pattern(candidates)
    if pattern is None:
        return []

    hits_lower: List[str] = [m.group(0).lower() for m in pattern.finditer(query)]
    if not hits_lower:
        return []

    # Map back to canonical casing in the order they first appeared in query.
    canonical_by_lower = {s.lower(): s for s in candidates}
    seen: dict = {}
    out: List[str] = []
    for low in hits_lower:
        canonical = canonical_by_lower.get(low)
        if canonical and canonical not in seen:
            seen[canonical] = True
            out.append(canonical)
    return out


# ----------------------------------------------------------------------
# Re-ranker
# ----------------------------------------------------------------------

# Tunables. Section boosts are added to the underlying hybrid-search score,
# which sits in a [-0.05, +1.0] range in production. Boosts of this size
# are large enough to move chunks across the top-K cut but small enough
# that a strongly-relevant non-section match can still win.
_SECTION_MATCH_BOOST = 0.40    # exact section_name match
_SECTION_PATH_BOOST  = 0.20    # section appears anywhere in section_path
_DOC_NAME_PENALTY    = 0.0     # reserved for future: penalise wrong doc


def _hit_section(hit: dict) -> Tuple[str, str]:
    """Return (section_name, section_path) for a hit, lowercased + stripped."""
    section_name = (hit.get("section_name") or "").strip().lower()
    section_path = (hit.get("section_path") or "").strip().lower()
    return section_name, section_path


def boost_hits_by_sections(
        hits: List[dict],
        target_sections: List[str],
) -> List[dict]:
    """
    Re-rank a hybrid-search result list so chunks from `target_sections`
    move toward the top.

    Args:
        hits:            List of dicts as returned by pgvector_search /
                         keyword_search. Each carries 'score', 'section_name',
                         'section_path' at minimum.
        target_sections: Canonical section names detected by
                         detect_sections_in_query.

    Returns:
        New list sorted by boosted score (descending). Original hits are not
        mutated; we shallow-copy each dict so adding a 'boosted_score' field
        doesn't surprise callers later in the pipeline.
    """
    if not hits or not target_sections:
        return list(hits)

    targets_lower = {s.lower() for s in target_sections}

    boosted: List[dict] = []
    for h in hits:
        base_score = float(h.get("score") or 0.0)
        section_name, section_path = _hit_section(h)
        bonus = 0.0
        if section_name and section_name in targets_lower:
            bonus = _SECTION_MATCH_BOOST
        elif section_path and any(t in section_path for t in targets_lower):
            bonus = _SECTION_PATH_BOOST
        new_h = dict(h)
        new_h["boosted_score"] = base_score + bonus
        new_h["section_boost"] = bonus
        boosted.append(new_h)

    boosted.sort(key=lambda x: x.get("boosted_score", 0.0), reverse=True)
    return boosted


def apply_section_routing(
        query: str,
        hits: List[dict],
        doc_kind: Optional[str] = None,
) -> Tuple[List[dict], List[str]]:
    """
    One-shot helper: detect sections in the query, re-rank hits accordingly,
    and return (ranked_hits, detected_sections).

    Designed to be a single drop-in line in chat_worker after the hybrid
    search call, e.g.:

        from core.section_query_router import apply_section_routing
        _deduped_pv, _detected = apply_section_routing(safe_q, _deduped_pv)
        if _detected:
            logger.info(f"section routing: query mentions {_detected}")
    """
    detected = detect_sections_in_query(query, doc_kind=doc_kind)
    if not detected:
        return list(hits), []
    reranked = boost_hits_by_sections(hits, detected)
    return reranked, detected
