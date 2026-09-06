# SPDX-License-Identifier: MIT
# ============================================================
# P08 (R3) — edge-optimized evidence packing + dedup (§8.7)
# ============================================================

from grounding.evidence import Chunk, dedup_chunks, edge_pack, pack_evidence


def _c(id, score, text=None):
    return Chunk(id=id, score=score, text=text if text is not None else id)


def test_dedup_drops_near_identical_text():
    chunks = [
        _c("a", 0.9, "the retry backoff uses exponential delay between attempts"),
        _c("b", 0.8, "the retry backoff uses exponential delay between attempts"),  # dup
        _c("c", 0.7, "a completely different chunk about caching layers here"),
    ]
    out = dedup_chunks(chunks)
    ids = [c.id for c in out]
    assert ids == ["a", "c"]  # first occurrence kept, dup 'b' removed


def test_dedup_short_text_compared_whole():
    chunks = [_c("a", 0.9, "x=1"), _c("b", 0.8, "x=1"), _c("c", 0.7, "y=2")]
    assert [c.id for c in dedup_chunks(chunks)] == ["a", "c"]


def test_edge_pack_places_best_at_edges():
    # ranks best->worst: 0,1,2,3,4  -> positions 0,2,4,3,1
    chunks = [_c(f"r{i}", 1.0 - i * 0.1) for i in range(5)]
    out = edge_pack(chunks)
    ids = [c.id for c in out]
    # best (r0) first, second-best (r1) last
    assert ids[0] == "r0"
    assert ids[-1] == "r1"
    assert set(ids) == {"r0", "r1", "r2", "r3", "r4"}


def test_pack_evidence_caps_dedups_and_edge_orders():
    chunks = [
        _c("dup1", 0.95, "same body text here for dedup"),
        _c("dup2", 0.5, "same body text here for dedup"),   # removed by dedup
        _c("b", 0.9, "unique b"),
        _c("c", 0.85, "unique c"),
        _c("d", 0.8, "unique d"),
        _c("e", 0.7, "unique e"),
        _c("f", 0.6, "unique f"),
        _c("g", 0.55, "unique g"),   # dropped by max_chunks
    ]
    out = pack_evidence(chunks, max_chunks=6)
    ids = [c.id for c in out]
    assert "dup2" not in ids            # deduped
    assert len(out) == 6                # capped
    assert ids[0] == "dup1"             # highest score at leading edge


def test_pack_empty_safe():
    assert pack_evidence([]) == []
    assert dedup_chunks([]) == []
    assert edge_pack([]) == []
