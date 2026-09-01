# SPDX-License-Identifier: Apache-2.0
# ============================================================
# validate_kb_pipeline.py
#
# A/B validation for the KB ingestion + retrieval improvements:
#   1. section_promoter — recovers ## headings from plain-text section titles
#   2. structure_scorer — quality verdict on the resulting chunk set
#   3. section_query_router — re-ranks search results by query-named section
#   4. KB_DOC_PROMPT — strict grounding rules
#
# Compares the BEFORE state (no promoter, no router) against the AFTER state
# (both active) on the SettleNXT release-note sample shipped with this repo.
#
# Run:
#   python scripts/validate_kb_pipeline.py
#
# The script touches NO production data. It exercises only the in-memory
# parser -> promoter -> chunker -> scorer pipeline plus a synthetic re-rank.
# ============================================================

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Allow running from anywhere in the repo without setting PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Sample document — the canonical AiNxt SettleNXT-RUPAY-1.0.44 release note used
# in the retrieval evaluation that motivated these changes.
_SAMPLE_MD = Path("D:/ainxt_docs/kb_docs/ac313e30-d0f5-4023-a4e6-8091a50a003a.md")

# Baseline queries that surfaced the original failures.
_BASELINE_QUERIES: List[str] = [
    "give the Prerequisites followed in SettleNXT-RUPAY-1.0.40_v1.0 from knowledgebase",
    "give the Prerequisites followed in SettleNXT-RUPAY release note from knowledgebase",
    "tell the release summary from settle NXT RUPAY release notes in knowledgebase",
]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _load_sample() -> str:
    if not _SAMPLE_MD.exists():
        print(f"FATAL: sample file not found at {_SAMPLE_MD}")
        sys.exit(2)
    return _SAMPLE_MD.read_text(encoding="utf-8")


def _quality_summary(text: str, structured_chunks: list, promoter_stats: Optional[dict] = None) -> dict:
    from core.structure_scorer import score_chunk_set
    score = score_chunk_set(
        text=text,
        structured_chunks=structured_chunks,
        promoter_stats=promoter_stats or {},
    )
    return score.to_dict()


def _hit_from_chunk(chunk: dict, score: float) -> dict:
    """Synthesize a pgvector_search-shaped hit from a structured chunk."""
    return {
        "text":         chunk.get("text") or "",
        "score":        score,
        "section_name": chunk.get("section_name") or "",
        "section_path": chunk.get("section_path") or "",
    }


def _simulate_retrieval(
        structured_chunks: list,
        query: str,
        top_k: int = 5,
) -> Tuple[List[dict], List[dict]]:
    """
    Approximate the production retrieval path WITHOUT pgvector by ranking
    chunks against the query using a simple lowercase keyword overlap score.

    This is intentionally crude — the goal is not to reproduce production
    ranking but to give the BEFORE and AFTER paths the same baseline scores
    so the section_query_router's effect is isolated and measurable.
    """
    q_terms = {t for t in query.lower().split() if len(t) > 2}
    hits: List[dict] = []
    for c in structured_chunks:
        text = (c.get("text") or "").lower()
        if not text:
            continue
        overlap = sum(1 for t in q_terms if t in text)
        score = overlap / max(len(q_terms), 1)
        hits.append(_hit_from_chunk(c, score))

    # BEFORE: pure similarity order, no section awareness
    hits_before = sorted(hits, key=lambda h: h["score"], reverse=True)[:top_k]

    # AFTER: same scores, then section router re-ranks
    from core.section_query_router import apply_section_routing
    ranked_all, detected = apply_section_routing(query, hits)
    hits_after = ranked_all[:top_k]

    return hits_before, hits_after


def _print_top_hits(label: str, hits: List[dict]) -> None:
    print(f"  {label}:")
    for i, h in enumerate(hits, 1):
        s = h.get("section_name") or "(no section)"
        score = h.get("boosted_score", h.get("score", 0.0))
        snippet = (h.get("text") or "").splitlines()
        snippet = snippet[0].strip() if snippet else ""
        if len(snippet) > 70:
            snippet = snippet[:67] + "..."
        print(f"    {i}. score={score:.2f}  [{s}]  {snippet}")


# ----------------------------------------------------------------------
# Stages
# ----------------------------------------------------------------------

def stage_chunk_quality(text: str) -> dict:
    """Compare chunk quality BEFORE and AFTER the promoter."""
    from core.section_promoter import promote_sections
    from store.docs_store import _chunk_document_structured

    print("== STAGE 1: Chunk quality (promoter on/off) ==")

    chunks_before = _chunk_document_structured(text)
    q_before = _quality_summary(text, chunks_before, {})
    print(f"  BEFORE promoter -> quality={q_before['quality']}  "
          f"coverage={q_before['section_coverage']:.0%}  "
          f"headings={q_before['heading_count']}  "
          f"chunks={q_before['total_chunks']}")

    promoted, promo_stats = promote_sections(text, doc_kind="RELEASE_NOTE")
    chunks_after = _chunk_document_structured(promoted)
    q_after = _quality_summary(promoted, chunks_after, promo_stats)
    print(f"  AFTER  promoter -> quality={q_after['quality']}  "
          f"coverage={q_after['section_coverage']:.0%}  "
          f"headings={q_after['heading_count']}  "
          f"chunks={q_after['total_chunks']}  "
          f"promoted={promo_stats['promoted']}")
    print(f"  Sections recovered: {sorted(set(promo_stats['matched_sections']))}")
    print()

    return {
        "before":             q_before,
        "after":              q_after,
        "promoted_lines":     promo_stats["promoted"],
        "sections_recovered": sorted(set(promo_stats["matched_sections"])),
        "promoted_text":      promoted,
        "chunks_after":       chunks_after,
    }


def stage_query_routing(query: str, chunks_after: list) -> dict:
    """Show the section_query_router effect on a single query."""
    print(f"== STAGE 2: Query — {query!r} ==")
    hits_before, hits_after = _simulate_retrieval(chunks_after, query)
    _print_top_hits("BEFORE re-rank", hits_before)
    _print_top_hits("AFTER  re-rank", hits_after)
    print()
    return {
        "query":        query,
        "top_before":   [{"section": h["section_name"], "score": h["score"]} for h in hits_before],
        "top_after":    [{"section": h["section_name"], "score": h["boosted_score"]} for h in hits_after],
    }


def stage_prompt_check() -> dict:
    """Snapshot the strict-grounding rules in KB_DOC_PROMPT for the report."""
    from agents.tools import KB_DOC_PROMPT
    rules = [
        ("verbatim only",            "STRICT GROUNDING RULES" in KB_DOC_PROMPT),
        ("no abbreviation expand",   "Do NOT expand abbreviations" in KB_DOC_PROMPT),
        ("no section blending",      "Do NOT blend content across sections" in KB_DOC_PROMPT),
        ("no fabricated headings",   "Do NOT invent sub-headings" in KB_DOC_PROMPT),
        ("no added narrative",       "Do NOT add interpretive narrative" in KB_DOC_PROMPT),
        ("version mismatch handler", "do not contain information for version" in KB_DOC_PROMPT.lower()),
        ("citations rule",           "Sources:" in KB_DOC_PROMPT),
    ]
    print("== STAGE 3: KB_DOC_PROMPT strict-grounding rules ==")
    for label, ok in rules:
        print(f"  [{'OK' if ok else 'MISSING'}]  {label}")
    print()
    return {"rules": {label: ok for label, ok in rules}}


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def main() -> int:
    text = _load_sample()
    report: dict = {}

    chunk_stage = stage_chunk_quality(text)
    report["chunk_quality"] = {
        k: v for k, v in chunk_stage.items() if k not in ("promoted_text", "chunks_after")
    }

    chunks_after = chunk_stage["chunks_after"]
    report["queries"] = []
    for q in _BASELINE_QUERIES:
        report["queries"].append(stage_query_routing(q, chunks_after))

    report["prompt"] = stage_prompt_check()

    # Top-line verdict ----------------------------------------------------
    b = report["chunk_quality"]["before"]
    a = report["chunk_quality"]["after"]
    delta_cov = a["section_coverage"] - b["section_coverage"]
    print("== SUMMARY ==")
    print(f"  section_coverage  {b['section_coverage']:.0%} -> {a['section_coverage']:.0%}  "
          f"(delta {delta_cov:+.0%})")
    print(f"  quality           {b['quality']} -> {a['quality']}")
    print(f"  sections recovered  {len(report['chunk_quality']['sections_recovered'])}")
    print(f"  prompt rules in place  "
          f"{sum(report['prompt']['rules'].values())}/{len(report['prompt']['rules'])}")

    # Write report JSON for CI / archive.
    out = _REPO_ROOT / "scripts" / "validate_kb_pipeline.report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nFull report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
