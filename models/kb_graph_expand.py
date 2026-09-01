# SPDX-License-Identifier: Apache-2.0
"""
KB graph expansion — multi-hop discovery via kb_edges.

Used by the Fast tier (rank/expand only) and by the §8y dependency-leak signal.
Never overrides verbatim source text — only surfaces additional chunks the
reasoner might otherwise miss, and lets the gate decide whether to escalate.
"""

from __future__ import annotations

from typing import Iterable

from core.logger import logger


def neighbors_for_chunks(
    chunk_ids: Iterable[str],
    product_id: str | None,
    spec_version: str | None,
    max_hops: int = 1,
    limit: int = 16,
) -> list[dict]:
    """
    Return chunks within `max_hops` of any input chunk via entity edges,
    scoped by product+version. Each output dict contains chunk_id, content,
    section_path, file_path, plus an `edge_hops` field for trace.
    """
    chunk_ids = [c for c in chunk_ids if c]
    if not chunk_ids:
        return []
    try:
        from db.database import VectorReadSessionLocal
        from sqlalchemy import text as _sql

        frontier = set(chunk_ids)
        all_visited = set(frontier)
        hop_table: dict[str, int] = {c: 0 for c in frontier}

        db = VectorReadSessionLocal()
        try:
            for hop in range(1, max_hops + 1):
                if not frontier:
                    break
                params = {
                    "ids": list(frontier),
                    "pid": product_id,
                    "sv":  spec_version,
                    "lim": limit * 4,
                }
                # entity edges only — dependency/version edges are walked separately.
                rows = db.execute(_sql("""
                    SELECT DISTINCT dst_chunk_id
                    FROM kb_edges
                    WHERE edge_type = 'entity'
                      AND (src_chunk_id = ANY(:ids) OR dst_chunk_id = ANY(:ids))
                      AND (:pid::uuid IS NULL OR product_id = :pid)
                      AND (:sv  IS NULL OR spec_version = :sv)
                    LIMIT :lim
                """), params).fetchall()
                new_frontier = set()
                for r in rows:
                    cid = str(r[0]) if r[0] else None
                    if cid and cid not in all_visited:
                        new_frontier.add(cid)
                        hop_table[cid] = hop
                all_visited |= new_frontier
                frontier = new_frontier

            new_ids = [c for c in all_visited if c not in set(chunk_ids)]
            if not new_ids:
                return []
            rows = db.execute(_sql("""
                SELECT id, content, file_path, section_path, parent_chunk_id
                FROM document_embeddings
                WHERE id = ANY(:ids)
                LIMIT :lim
            """), {"ids": new_ids, "lim": limit}).fetchall()
        finally:
            db.close()

        out: list[dict] = []
        for r in rows:
            cid = str(r[0])
            out.append({
                "chunk_id":        cid,
                "text":            r[1] or "",
                "file_path":       r[2] or "",
                "section_path":    r[3] or "",
                "parent_chunk_id": str(r[4]) if r[4] else None,
                "edge_hops":       hop_table.get(cid, max_hops),
                "score":           max(0.0, 0.75 - 0.15 * hop_table.get(cid, max_hops)),
                "source":          "kb_graph",
            })
        return out
    except Exception as e:
        logger.warning(f"kb_graph_expand.neighbors_for_chunks failed: {e}")
        return []


def has_dependency_leak(
    retrieved_chunk_ids: list[str],
    product_id: str | None,
    spec_version: str | None,
) -> bool:
    """
    §8y signal #2: returns True iff any retrieved chunk has an entity edge to a
    chunk that is NOT in the retrieved set. The escalation gate uses this as a
    fast boolean — if there's a leak, escalate to Coverage.
    """
    if not retrieved_chunk_ids:
        return False
    try:
        from db.database import VectorReadSessionLocal
        from sqlalchemy import text as _sql

        db = VectorReadSessionLocal()
        try:
            params = {
                "ids": retrieved_chunk_ids,
                "pid": product_id,
                "sv":  spec_version,
            }
            row = db.execute(_sql("""
                SELECT 1
                FROM kb_edges
                WHERE edge_type = 'entity'
                  AND src_chunk_id = ANY(:ids)
                  AND dst_chunk_id IS NOT NULL
                  AND NOT (dst_chunk_id = ANY(:ids))
                  AND (:pid::uuid IS NULL OR product_id = :pid)
                  AND (:sv  IS NULL OR spec_version = :sv)
                LIMIT 1
            """), params).fetchone()
            return row is not None
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"kb_graph_expand.has_dependency_leak failed: {e}")
        return False


# ============================================================
# Part U14 (2026-06-08) — typed lineage / impact traversal
# ============================================================
def neighbors_for_doc(
    doc_id: str,
    relations: list[str] | None = None,
    product_id: str | None = None,
    spec_version: str | None = None,
    max_hops: int = 2,
    limit: int = 12,
) -> list[dict]:
    """
    Walk kb_edges from `doc_id` along TYPED dependency / version / structure
    edges (NOT entity co-occurrence — that's neighbors_for_chunks). Used by
    the impact-query branch in hybrid_retriever to answer:

        "Why was X introduced?"     → walk approved_by / supersedes
        "What's affected by Y?"     → walk references / governs
        "What changed in Z?"        → walk supersedes / version edges

    Source: AiNxt_Retrieval_Discussion_Summary.docx §10–§11.

    `relations` filters props.relation. None = walk every allowed relation.

    Returns: list of dicts:
        {
          "dst_doc_id":   "<uuid>",
          "dst_doc_name": "<doc name>",
          "relation":     "approved_by" | ...,
          "evidence":     "<sentence captured at index time>",
          "hops":         <int>,
          "source":       "kb_graph_lineage",
          "score":        <float>,
        }
    Scoring decays with hops so 1-hop results outrank deeper ones at merge time.
    """
    if not doc_id:
        return []
    try:
        from db.database import VectorReadSessionLocal
        from sqlalchemy import text as _sql

        # BFS frontier of doc_ids walked so far.
        frontier: set[str] = {doc_id}
        visited: set[str] = {doc_id}
        # (dst_doc_id, relation, evidence, hop) tuples encountered.
        edges_seen: list[tuple[str, str, str, int]] = []

        db = VectorReadSessionLocal()
        try:
            for hop in range(1, max_hops + 1):
                if not frontier:
                    break
                params = {
                    "ids": list(frontier),
                    "pid": product_id,
                    "sv":  spec_version,
                    "rels": list(relations) if relations else None,
                    "lim": limit * 4,
                }
                # Walk both directions: src→dst AND dst→src on the same edge
                # set so "X approved by Y" and "Y approved X" both surface.
                rows = db.execute(_sql("""
                    SELECT
                        CASE WHEN src_doc_id = ANY(:ids)
                             THEN dst_doc_id
                             ELSE src_doc_id
                        END AS other_doc_id,
                        (props->>'relation')  AS relation,
                        COALESCE(props->>'evidence', '') AS evidence
                    FROM kb_edges
                    WHERE edge_type IN ('dependency', 'version', 'structure')
                      AND (props ? 'relation')
                      AND (
                            src_doc_id = ANY(:ids)
                         OR dst_doc_id = ANY(:ids)
                      )
                      AND (:rels::text[] IS NULL OR (props->>'relation') = ANY(:rels))
                      AND (:pid::uuid IS NULL OR product_id = :pid)
                      AND (:sv  IS NULL OR spec_version = :sv)
                    LIMIT :lim
                """), params).fetchall()

                new_frontier: set[str] = set()
                for r in rows:
                    other = str(r[0]) if r[0] else None
                    if not other or other in visited:
                        continue
                    edges_seen.append((other, r[1] or "", r[2] or "", hop))
                    new_frontier.add(other)
                    visited.add(other)
                frontier = new_frontier

            if not edges_seen:
                return []

            # Resolve doc display names from PGS01.knowledge_docs. Cross-DB
            # join is acceptable here because impact queries are rare; we
            # pay the one-time round-trip per call, not per chunk.
            unique_dst = list({e[0] for e in edges_seen})
        finally:
            db.close()

        # Doc-name lookup on PGS01.
        try:
            from db.database import SessionLocal
            _mdb = SessionLocal()
            try:
                name_rows = _mdb.execute(_sql("""
                    SELECT id::text, name, source_type
                    FROM knowledge_docs
                    WHERE id::text = ANY(:ids)
                """), {"ids": unique_dst}).fetchall()
                doc_name_map = {r[0]: {"name": r[1], "source_type": r[2]} for r in name_rows}
            finally:
                _mdb.close()
        except Exception as _ne:
            logger.debug(f"neighbors_for_doc: doc-name lookup skipped: {_ne}")
            doc_name_map = {}

        out: list[dict] = []
        for dst_id, relation, evidence, hop in edges_seen[:limit]:
            _meta = doc_name_map.get(dst_id, {})
            out.append({
                "dst_doc_id":   dst_id,
                "dst_doc_name": _meta.get("name") or "(unknown doc)",
                "source_type":  _meta.get("source_type"),
                "relation":     relation,
                "evidence":     evidence,
                "hops":         hop,
                "source":       "kb_graph_lineage",
                "score":        max(0.0, 0.8 - 0.2 * (hop - 1)),
            })
        return out
    except Exception as e:
        logger.warning(f"kb_graph_expand.neighbors_for_doc failed: {e}")
        return []
