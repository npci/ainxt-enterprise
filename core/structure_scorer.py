# SPDX-License-Identifier: Apache-2.0
# ============================================================
# STRUCTURE SCORER — pre-chunking and post-chunking quality metrics
#
# Purpose:
#   Give operators an early-warning signal when an uploaded document arrives
#   with poor markdown structure. Documents with no headings produce chunks
#   with empty section_path, which is the root cause of cross-section
#   blending and fabricated citations observed in the retrieval evaluation.
#
#   We compute a small set of metrics per upload and log them so the signal
#   is visible without adding a new datastore. The metrics also feed the
#   admin diagnostics endpoint and future dashboards.
#
# Where this is called:
#   store/docs_store.upload_doc() invokes score_chunk_set() right after
#   _chunk_document_structured(). The score is logged at INFO when good,
#   WARNING when the document falls below the configured threshold so
#   on-call sees it without scanning every upload.
# ============================================================

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List, Optional

from core.logger import logger


# ----------------------------------------------------------------------
# Tunables
#
# These are intentionally generous defaults. Tighten as you collect data
# from production uploads. The goal is to alert on documents that will
# clearly degrade retrieval — not to nag on every minor structure miss.
# ----------------------------------------------------------------------

_GOOD_SECTION_COVERAGE = 0.75     # ≥ 75% of leaf chunks carry section_path
_WARN_SECTION_COVERAGE = 0.40     # < 40% triggers WARN-level log
_MIN_HEADINGS_FOR_GOOD = 3        # docs with < 3 headings rarely chunk well

_HEADING_RE = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)


@dataclass
class StructureScore:
    """
    Aggregate quality signal for a single document.

    Fields are surface-level so the dict form serialises directly into log
    lines and the future diagnostics endpoint without further mapping.
    """
    total_chunks:        int           # all entries returned by chunker
    leaf_chunks:         int           # non-parent chunks (what gets embedded)
    parent_chunks:       int
    chunks_with_path:    int           # leaves whose section_path is non-empty
    section_coverage:    float         # chunks_with_path / leaf_chunks
    heading_count:       int           # number of "#…" lines in source text
    distinct_sections:   int           # unique section_path values among leaves
    matched_vocab:       List[str]     # canonical section names from promoter
    promoted_lines:      int           # lines rewritten by section_promoter
    quality:             str           # "GOOD" | "DEGRADED" | "POOR"
    reasons:             List[str]     # short human reasons for the verdict

    def to_dict(self) -> dict:
        return asdict(self)


def _count_headings(text: Optional[str]) -> int:
    if not text:
        return 0
    return len(_HEADING_RE.findall(text))


def score_chunk_set(
        *,
        text:           Optional[str],
        structured_chunks: List[dict],
        promoter_stats: Optional[dict] = None,
) -> StructureScore:
    """
    Compute structural quality for the chunk set produced by
    docs_store._chunk_document_structured().

    Args:
        text:              The (possibly promoter-rewritten) markdown that
                           was fed to the chunker. Used to count "#" headings.
        structured_chunks: The list-of-dict output of the structured chunker.
                           Each entry has at minimum: text, section_path,
                           is_parent, parent_idx.
        promoter_stats:    Optional dict returned by section_promoter — lets
                           us attribute "promoted X sections" in the log line.

    Returns:
        StructureScore — dataclass with quality tier + reasons.
    """
    promoter_stats = promoter_stats or {}
    total = len(structured_chunks or [])
    parents = [c for c in structured_chunks or [] if c.get("is_parent")]
    leaves = [c for c in structured_chunks or [] if not c.get("is_parent")]
    leaf_count = len(leaves)

    # section_path coverage is computed on leaves only — parents are
    # whole-section roll-ups and always carry section_path, so including
    # them would inflate the ratio.
    with_path = sum(1 for c in leaves if (c.get("section_path") or "").strip())
    coverage = (with_path / leaf_count) if leaf_count else 0.0

    distinct = len({(c.get("section_path") or "") for c in leaves
                    if (c.get("section_path") or "").strip()})

    heading_count = _count_headings(text)

    # Verdict ------------------------------------------------------------
    reasons: List[str] = []
    if leaf_count == 0:
        quality = "POOR"
        reasons.append("no chunks produced")
    elif heading_count < _MIN_HEADINGS_FOR_GOOD and coverage < _WARN_SECTION_COVERAGE:
        quality = "POOR"
        reasons.append(f"only {heading_count} markdown heading(s)")
        reasons.append(f"section coverage {coverage:.0%} below {_WARN_SECTION_COVERAGE:.0%}")
    elif coverage < _WARN_SECTION_COVERAGE:
        quality = "POOR"
        reasons.append(f"section coverage {coverage:.0%} below {_WARN_SECTION_COVERAGE:.0%}")
    elif coverage < _GOOD_SECTION_COVERAGE:
        quality = "DEGRADED"
        reasons.append(f"section coverage {coverage:.0%} below target {_GOOD_SECTION_COVERAGE:.0%}")
    else:
        quality = "GOOD"

    return StructureScore(
        total_chunks=total,
        leaf_chunks=leaf_count,
        parent_chunks=len(parents),
        chunks_with_path=with_path,
        section_coverage=round(coverage, 3),
        heading_count=heading_count,
        distinct_sections=distinct,
        matched_vocab=list(promoter_stats.get("matched_sections", []) or []),
        promoted_lines=int(promoter_stats.get("promoted", 0) or 0),
        quality=quality,
        reasons=reasons,
    )


def log_score(score: StructureScore, *, doc_id: Optional[str] = None,
              filename: Optional[str] = None) -> None:
    """
    Emit one log line with the structural verdict.

    - GOOD     → INFO   (visible in normal aggregation)
    - DEGRADED → INFO   (visible but does not page on-call)
    - POOR     → WARNING (surfaces in alerting pipelines)

    Keeping the level-mapping here means callers don't have to reason about
    severity at each site that scores a document.
    """
    payload = score.to_dict()
    head = f"structure_scorer: doc={doc_id or '-'} filename={filename or '-'}"
    if score.quality == "POOR":
        logger.warning(f"{head} quality=POOR reasons={score.reasons} metrics={payload}")
    else:
        logger.info(f"{head} quality={score.quality} metrics={payload}")
