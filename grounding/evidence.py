# SPDX-License-Identifier: MIT
# ============================================================
# EvidencePack + Reciprocal Rank Fusion (RRF)
# ============================================================
#
# docs/architecture/08-retrieval-intelligence.md §8.3 (EvidencePack) and §8.6
# (RRF). Two pieces:
#
#   1. EvidencePack — a typed retrieval result so grounding (Phase 14) and
#      citations can reference chunks by id.
#   2. rrf_fuse() — rank-based, scale-free fusion. The current production
#      merge (hybrid_search.merge_and_rerank) uses score-MAX dedup, which
#      conflates incomparable vector-cosine and BM25 scales. RRF fuses RANKS,
#      so heterogeneous strategies combine on equal footing.
#
# This is a PURE, standalone helper (stdlib only). It is NOT yet wired into the
# live retriever — adoption is a later, flag-gated, eval-gated step. Providing
# it as a tested pure function first is the fail-safe path.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Chunk:
    """One retrieved unit with provenance (docs/architecture/08 §8.3)."""

    id: str
    text: str = ""
    score: float = 0.0
    source: str = ""          # e.g. "repo:client.py#L120" | "kb:Policy v3"
    strategy: str = ""        # "vector" | "keyword" | "symbol" | "vector+rerank"
    version: str = ""
    scope: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Chunk":
        """Adapt a live retriever dict (hybrid_retriever/hybrid_search return
        bare dicts keyed by chunk_id/id/text/score) into a typed Chunk. Makes
        future adoption a one-liner instead of a second ad-hoc mapping."""
        return cls(
            id=str(d.get("chunk_id") or d.get("id") or ""),
            text=d.get("text", "") or "",
            score=float(d.get("score", 0.0) or 0.0),
            source=d.get("source") or d.get("file_path", "") or "",
            strategy=d.get("strategy", "") or "",
            version=str(d.get("version", "") or ""),
            scope=str(d.get("scope", "") or ""),
        )


@dataclass
class EvidencePack:
    """Typed retrieval output consumed by grounding + prompt construction."""

    query: str = ""
    rewritten: List[str] = field(default_factory=list)
    chunks: List[Chunk] = field(default_factory=list)
    coverage: float = 0.0     # sufficiency (coverage_gate)
    confidence: float = 0.0

    def chunk_ids(self) -> List[str]:
        return [c.id for c in self.chunks]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def rrf_fuse(
    ranked_lists: List[List[str]],
    *,
    k: int = 60,
    top_n: Optional[int] = None,
) -> List[str]:
    """Fuse multiple ranked id-lists via Reciprocal Rank Fusion.

    RRF(d) = sum over lists of 1 / (k + rank_in_list(d))   (rank is 0-based here)

    Rank-based → scale-free, so vector/keyword/symbol lists combine fairly
    regardless of their native score scales. `k` dampens the contribution of
    low-ranked items (60 is the standard default). Returns ids sorted by fused
    score (desc); ties broken by best (lowest) rank then id for determinism.
    """
    scores: Dict[str, float] = {}
    best_rank: Dict[str, int] = {}
    for lst in ranked_lists or []:
        for rank, doc_id in enumerate(lst):
            if not doc_id:
                continue
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
            if doc_id not in best_rank or rank < best_rank[doc_id]:
                best_rank[doc_id] = rank

    ordered = sorted(
        scores.keys(),
        key=lambda d: (-scores[d], best_rank[d], d),
    )
    return ordered[:top_n] if top_n is not None else ordered


def dedup_chunks(chunks: List[Chunk], *, min_len: int = 12) -> List[Chunk]:
    """Drop near-identical chunks (docs/architecture/08 §8.7). Redundant evidence
    wastes the KB-slot budget and biases the model. Keeps the FIRST occurrence
    (higher-ranked by caller order) of each normalized text; short texts are
    compared whole, long texts by a normalized fingerprint. Never raises."""
    try:
        seen: set = set()
        out: List[Chunk] = []
        for c in chunks or []:
            norm = " ".join((c.text or "").lower().split())
            key = norm if len(norm) < min_len else norm[:200]
            if key and key in seen:
                continue
            seen.add(key)
            out.append(c)
        return out
    except Exception:  # noqa: BLE001
        return list(chunks or [])


def edge_pack(chunks: List[Chunk]) -> List[Chunk]:
    """Order chunks so the HIGHEST-scored evidence sits at the EDGES of the slot,
    where model attention is strongest ("lost in the middle", §8.7). Given inputs
    ranked best→worst, emit best first, second-best last, working inward:
    ranks [0,1,2,3,4] → positions [0,2,4,3,1]. Pure, never raises."""
    try:
        ranked = sorted(chunks or [], key=lambda c: c.score, reverse=True)
        head: List[Chunk] = []
        tail: List[Chunk] = []
        for i, c in enumerate(ranked):
            (head if i % 2 == 0 else tail).append(c)
        return head + list(reversed(tail))
    except Exception:  # noqa: BLE001
        return list(chunks or [])


def pack_evidence(
    chunks: List[Chunk],
    *,
    max_chunks: int = 6,
    dedup: bool = True,
) -> List[Chunk]:
    """Full §8.7 packing: dedup near-identical → cap to max_chunks by score →
    edge-optimize order. The dropped-by-cap count is recoverable as
    len(input) - len(output) for telemetry. Never raises."""
    try:
        work = dedup_chunks(chunks) if dedup else list(chunks or [])
        capped = sorted(work, key=lambda c: c.score, reverse=True)[:max_chunks]
        return edge_pack(capped)
    except Exception:  # noqa: BLE001
        return list(chunks or [])[:max_chunks]
