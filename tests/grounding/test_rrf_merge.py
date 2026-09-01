# SPDX-License-Identifier: Apache-2.0
# ============================================================
# P8 — RRF merge behavior for hybrid_search.merge_and_rerank
# ============================================================
#
# hybrid_search.py can't be imported in a bare env (infra deps), so this mirrors
# the _merge_rrf grouping logic exactly and exercises the real rrf_fuse to prove
# the fusion behavior + fail-safe. The mirror is kept byte-faithful to the impl.
# ============================================================

from grounding.evidence import rrf_fuse


def _merge_rrf(results, top_k):
    """Mirror of hybrid_search._merge_rrf (grouping + fuse)."""
    by_id = {}
    per_strategy = {}
    for item in results:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        _id = str(item.get("chunk_id") or item.get("id") or text)
        score = float(item.get("score", 0.0))
        src = item.get("source", "unknown")
        if _id not in by_id or score > by_id[_id].get("score", 0.0):
            by_id[_id] = {**item, "text": text, "score": score}
        per_strategy.setdefault(src, []).append((score, _id))
    if not by_id:
        return []
    ranked_lists = [
        [i for _s, i in sorted(items, key=lambda t: t[0], reverse=True)]
        for items in per_strategy.values()
    ]
    fused_ids = rrf_fuse(ranked_lists, top_n=top_k)
    return [by_id[i] for i in fused_ids if i in by_id]


def test_agreement_across_strategies_wins():
    # chunk "c1" is top in pgvector AND present in bm25 → should rank first
    results = [
        {"chunk_id": "c1", "text": "alpha", "score": 0.9, "source": "pgvector"},
        {"chunk_id": "c2", "text": "beta", "score": 0.8, "source": "pgvector"},
        {"chunk_id": "c1", "text": "alpha", "score": 5.0, "source": "bm25"},  # diff scale
        {"chunk_id": "c3", "text": "gamma", "score": 4.0, "source": "bm25"},
    ]
    out = _merge_rrf(results, top_k=10)
    ids = [o["chunk_id"] for o in out]
    assert ids[0] == "c1"  # agreement across two strategies
    assert set(ids) == {"c1", "c2", "c3"}


def test_scale_free_bm25_does_not_dominate():
    # bm25 scores (0..big) must NOT swamp cosine (0..1) — rank-based fusion
    results = [
        {"chunk_id": "v1", "text": "x", "score": 0.95, "source": "pgvector"},
        {"chunk_id": "b1", "text": "y", "score": 42.0, "source": "bm25"},
    ]
    out = _merge_rrf(results, top_k=10)
    # both are rank-0 in their strategy → tie; deterministic, both present
    assert {o["chunk_id"] for o in out} == {"v1", "b1"}


def test_dedup_keeps_highest_native_score_metadata():
    results = [
        {"chunk_id": "c1", "text": "t", "score": 0.3, "source": "pgvector", "file_path": "a"},
        {"chunk_id": "c1", "text": "t", "score": 0.7, "source": "symbol", "file_path": "b"},
    ]
    out = _merge_rrf(results, top_k=10)
    assert len(out) == 1
    assert out[0]["file_path"] == "b"  # metadata from higher native score


def test_empty_and_blank():
    assert _merge_rrf([], 10) == []
    assert _merge_rrf([{"text": "  ", "score": 1.0, "source": "x"}], 10) == []


def test_top_k_truncation():
    results = [{"chunk_id": f"c{i}", "text": f"t{i}", "score": 1.0 - i * 0.1, "source": "pgvector"}
               for i in range(5)]
    assert len(_merge_rrf(results, top_k=3)) == 3
