# SPDX-License-Identifier: MIT
# ============================================================
# KNOWLEDGE GRAPH ROUTER  (mounted at /ainxt/v1/api/graph)
#
# Serves both CLIs (v1 + v2) and the web UI. Unified code+docs graph.
#   POST /graph/build            — enqueue a build (repo:/kb:/cross:)
#   GET  /graph/status/{gid}     — build state + counts
#   GET  /graph/explore          — RBAC-filtered subgraph (recursive CTE) for viz
#   POST /graph/query            — multi-hop traversal answer
#   GET  /graph/domain           — LLM-clustered business domains
#   GET  /graph/node/{node_id}   — node detail + 1-hop neighbours
#
# RBAC is enforced as a SQL WHERE on BOTH the node and edge tables (never a
# Python post-filter): department scoping mirrors document_embeddings, plus a
# band-clearance gate (clearance = 6 - ad_level; node visible if min_band_level
# <= clearance, admins bypass).
# ============================================================

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text as _sql

from auth.dependencies import get_current_user
from core.logger import logger

router = APIRouter(prefix="/graph", tags=["knowledge_graph"])


def _db():
    from db.database import SessionLocal
    return SessionLocal()


def _user_ctx(user: dict) -> dict:
    try:
        ad = int(user.get("ad_level", 6))
    except Exception:
        ad = 6
    return {
        "is_admin": bool(user.get("role") == "admin"),
        "user_dept": user.get("department") or "",
        "user_clearance": max(0, 6 - ad),
    }


def _node_acl(alias: str = "n") -> str:
    return (f"(:is_admin OR (({alias}.department IS NULL OR {alias}.department = '' "
            f"OR {alias}.department = :user_dept) AND {alias}.min_band_level <= :user_clearance))")


def _edge_acl(alias: str = "e") -> str:
    return f"(:is_admin OR {alias}.min_band_level <= :user_clearance)"


@router.get("/list")
async def list_graphs(current_user=Depends(get_current_user)):
    """All graphs the caller can see (RBAC-filtered), with node counts + build state.
    Powers the web dashboard's graph picker."""
    ctx = _user_ctx(current_user)
    db = _db()
    try:
        rows = db.execute(_sql(f"""
            SELECT n.graph_id,
                   count(*)                                          AS nodes,
                   count(*) FILTER (WHERE n.source_type = 'code')    AS code_nodes,
                   count(*) FILTER (WHERE n.source_type = 'doc')     AS doc_nodes
            FROM knowledge_graph_nodes n
            WHERE {_node_acl()}
            GROUP BY n.graph_id
            ORDER BY n.graph_id
        """), ctx).fetchall()
        st = {}
        try:
            for s in db.execute(_sql(
                "SELECT graph_id, status, last_built_at FROM knowledge_graph_build_status"
            )).fetchall():
                st[s[0]] = {"status": s[1], "last_built_at": str(s[2]) if s[2] else None}
        except Exception:
            pass
    finally:
        db.close()
    graphs = []
    for r in rows:
        gid = r[0]
        graphs.append({
            "graph_id":      gid,
            "kind":          gid.split(":", 1)[0] if ":" in gid else "other",
            "nodes":         r[1],
            "code_nodes":    r[2],
            "doc_nodes":     r[3],
            "status":        st.get(gid, {}).get("status"),
            "last_built_at": st.get(gid, {}).get("last_built_at"),
        })
    return {"graphs": graphs}


class BuildReq(BaseModel):
    graph_id: str
    trigger_domain: bool = False
    trigger_cross: Optional[str] = None


class QueryReq(BaseModel):
    question: str
    graph_id: str
    max_hops: int = 2


@router.post("/build")
async def build_graph(req: BuildReq, current_user=Depends(get_current_user)):
    from core.job_queue import enqueue_job, Q_KB
    try:
        job_id = enqueue_job(
            "workers.knowledge_graph_worker.build_graph_job",
            {"graph_id": req.graph_id, "trigger_domain": req.trigger_domain,
             "trigger_cross": req.trigger_cross,
             "triggered_by": current_user.get("sub")},
            queue_name=Q_KB, timeout=3600, retry_count=1,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"queue unavailable: {e}")
    return {"job_id": job_id, "status": "queued", "graph_id": req.graph_id}


@router.get("/status/{graph_id:path}")
async def graph_status(graph_id: str, current_user=Depends(get_current_user)):
    db = _db()
    try:
        row = db.execute(_sql(
            "SELECT status, job_id, code_nodes, doc_nodes, cross_edges, error, last_built_at "
            "FROM knowledge_graph_build_status WHERE graph_id = :gid"), {"gid": graph_id}).fetchone()
        ncount = db.execute(_sql("SELECT count(*) FROM knowledge_graph_nodes WHERE graph_id = :gid"),
                            {"gid": graph_id}).scalar() or 0
        ecount = db.execute(_sql("SELECT count(*) FROM knowledge_graph_edges WHERE graph_id = :gid"),
                            {"gid": graph_id}).scalar() or 0
    finally:
        db.close()
    if not row and not ncount:
        return {"graph_id": graph_id, "status": "not_built", "nodes": 0, "edges": 0}
    return {
        "graph_id": graph_id,
        "status": (row[0] if row else "done"),
        "job_id": (row[1] if row else None),
        "code_nodes": (row[2] if row else 0),
        "doc_nodes": (row[3] if row else 0),
        "cross_edges": (row[4] if row else 0),
        "error": (row[5] if row else None),
        "last_built_at": (str(row[6]) if row and row[6] else None),
        "nodes": ncount, "edges": ecount,
    }


@router.get("/explore")
async def explore(
    graph_id: str,
    seed: Optional[str] = None,
    depth: int = Query(1, ge=1, le=3),
    limit: int = Query(50, ge=1, le=200),
    node_types: Optional[str] = None,
    edge_types: Optional[str] = None,
    fmt: Optional[str] = Query(None, alias="format"),
    current_user=Depends(get_current_user),
):
    ctx = _user_ctx(current_user)
    db = _db()
    try:
        seeds: Optional[list[str]] = None
        if seed:
            rows = db.execute(_sql(
                f"SELECT node_id FROM knowledge_graph_nodes n WHERE graph_id = :gid "
                f"AND lower(name) = lower(:seed) AND {_node_acl()} LIMIT 20"),
                {"gid": graph_id, "seed": seed, **ctx}).fetchall()
            seeds = [r[0] for r in rows]
            if not seeds:
                rows = db.execute(_sql(
                    f"SELECT node_id FROM knowledge_graph_nodes n WHERE graph_id = :gid "
                    f"AND lower(name) LIKE lower(:seed) AND {_node_acl()} LIMIT 20"),
                    {"gid": graph_id, "seed": f"%{seed}%", **ctx}).fetchall()
                seeds = [r[0] for r in rows]

        if seeds:
            reached = db.execute(_sql(f"""
                WITH RECURSIVE trav(node_id, depth) AS (
                    SELECT node_id, 0 FROM knowledge_graph_nodes
                    WHERE graph_id = :gid AND node_id = ANY(:seeds)
                    UNION
                    SELECT e.dst_node_id, t.depth + 1
                    FROM knowledge_graph_edges e JOIN trav t ON e.src_node_id = t.node_id
                    WHERE e.graph_id = :gid AND t.depth < :depth AND {_edge_acl()}
                )
                SELECT DISTINCT node_id FROM trav LIMIT :limit
            """), {"gid": graph_id, "seeds": seeds, "depth": depth, "limit": limit, **ctx}).fetchall()
            ids = [r[0] for r in reached]
            node_rows = db.execute(_sql(
                f"SELECT node_id, node_type, name, source_type, summary FROM knowledge_graph_nodes n "
                f"WHERE graph_id = :gid AND node_id = ANY(:ids) AND {_node_acl()}"),
                {"gid": graph_id, "ids": ids, **ctx}).fetchall()
        else:
            p = {"gid": graph_id, "limit": limit, **ctx}
            nt = ""
            if node_types:
                nt = "AND node_type = ANY(:nts)"
                p["nts"] = [s.strip() for s in node_types.split(",") if s.strip()]
            node_rows = db.execute(_sql(
                f"SELECT node_id, node_type, name, source_type, summary FROM knowledge_graph_nodes n "
                f"WHERE graph_id = :gid AND {_node_acl()} {nt} "
                f"ORDER BY (metadata->>'centrality') DESC NULLS LAST LIMIT :limit"), p).fetchall()

        ids = [r[0] for r in node_rows]
        nodes = [{"id": r[0], "type": r[1], "name": r[2], "source_type": r[3], "summary": r[4]}
                 for r in node_rows]
        ep = {"gid": graph_id, "ids": ids, **ctx}
        et = ""
        if edge_types:
            et = "AND edge_type = ANY(:ets)"
            ep["ets"] = [s.strip() for s in edge_types.split(",") if s.strip()]
        edge_rows = db.execute(_sql(
            f"SELECT src_node_id, dst_node_id, edge_type, weight FROM knowledge_graph_edges e "
            f"WHERE graph_id = :gid AND src_node_id = ANY(:ids) AND dst_node_id = ANY(:ids) "
            f"AND {_edge_acl()} {et} LIMIT 500"), ep).fetchall() if ids else []
        edges = [{"src": r[0], "dst": r[1], "type": r[2], "weight": float(r[3])} for r in edge_rows]
    finally:
        db.close()

    if fmt == "graphml":
        return {"graphml": _to_graphml(nodes, edges)}
    return {"nodes": nodes, "edges": edges, "truncated": len(nodes) >= limit, "total_nodes": len(nodes)}


@router.post("/query")
async def query_graph(req: QueryReq, current_user=Depends(get_current_user)):
    import re as _re
    ctx = _user_ctx(current_user)
    hops = max(1, min(req.max_hops, 3))
    terms = list({w.lower() for w in _re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", req.question)})[:8]
    if not terms:
        return {"answer": "No searchable terms in question.", "matched": [], "sources": []}
    db = _db()
    try:
        seed_rows = db.execute(_sql(
            f"SELECT node_id, name FROM knowledge_graph_nodes n WHERE graph_id = :gid "
            f"AND lower(name) = ANY(:terms) AND {_node_acl()} LIMIT 20"),
            {"gid": req.graph_id, "terms": terms, **ctx}).fetchall()
        # Fuzzy fallback: an exact full-name match fails for multi-word node names
        # ("compliance gate", "LLM (Large Language Model)"). Match each term as a
        # substring of the node name so natural questions still find their seeds.
        if not seed_rows:
            like = " OR ".join(f"lower(n.name) LIKE :t{i}" for i in range(len(terms)))
            like_params = {f"t{i}": f"%{t}%" for i, t in enumerate(terms)}
            seed_rows = db.execute(_sql(
                f"SELECT node_id, name FROM knowledge_graph_nodes n WHERE graph_id = :gid "
                f"AND ({like}) AND {_node_acl()} LIMIT 20"),
                {"gid": req.graph_id, **like_params, **ctx}).fetchall()
        seeds = [r[0] for r in seed_rows]
        matched = [r[1] for r in seed_rows]
        if not seeds:
            return {"answer": "No graph nodes matched the question.", "matched": [], "sources": []}
        reached = db.execute(_sql(f"""
            WITH RECURSIVE trav(node_id, depth) AS (
                SELECT node_id, 0 FROM knowledge_graph_nodes
                WHERE graph_id = :gid AND node_id = ANY(:seeds)
                UNION
                SELECT e.dst_node_id, t.depth + 1
                FROM knowledge_graph_edges e JOIN trav t ON e.src_node_id = t.node_id
                WHERE e.graph_id = :gid AND t.depth < :hops AND {_edge_acl()}
            )
            SELECT DISTINCT n.name, n.node_type, n.source_ref, n.summary
            FROM trav tr JOIN knowledge_graph_nodes n
              ON n.graph_id = :gid AND n.node_id = tr.node_id
            WHERE {_node_acl()} LIMIT 60
        """), {"gid": req.graph_id, "seeds": seeds, "hops": hops, **ctx}).fetchall()
    finally:
        db.close()
    sources = [{"name": r[0], "type": r[1], "ref": r[2], "summary": r[3]} for r in reached]
    answer = (f"Matched {len(matched)} node(s): {', '.join(matched[:8])}. "
              f"Within {hops} hop(s): {len(sources)} related — "
              + ", ".join(s["name"] for s in sources[:20]))
    return {"answer": answer, "matched": matched, "sources": sources, "traversal_hops": hops}


@router.get("/domain")
async def domain(graph_id: str, current_user=Depends(get_current_user)):
    db = _db()
    try:
        rows = db.execute(_sql(
            "SELECT domain_name, description, member_node_ids, centroid "
            "FROM knowledge_graph_domains WHERE graph_id = :gid ORDER BY domain_name"),
            {"gid": graph_id}).fetchall()
    finally:
        db.close()
    return {"domains": [{"name": r[0], "description": r[1],
                         "member_count": len(r[2] or []), "centroid": r[3]} for r in rows]}


@router.get("/node/{node_id:path}")
async def node_detail(node_id: str, graph_id: str, current_user=Depends(get_current_user)):
    ctx = _user_ctx(current_user)
    db = _db()
    try:
        n = db.execute(_sql(
            f"SELECT node_id, node_type, name, source_type, source_ref, summary, language "
            f"FROM knowledge_graph_nodes n WHERE graph_id = :gid AND node_id = :nid AND {_node_acl()}"),
            {"gid": graph_id, "nid": node_id, **ctx}).fetchone()
        if not n:
            raise HTTPException(status_code=404, detail="node not found or not accessible")
        nbrs = db.execute(_sql(
            f"SELECT e.edge_type, e.dst_node_id, m.name FROM knowledge_graph_edges e "
            f"LEFT JOIN knowledge_graph_nodes m ON m.graph_id = e.graph_id AND m.node_id = e.dst_node_id "
            f"WHERE e.graph_id = :gid AND e.src_node_id = :nid AND {_edge_acl()} LIMIT 50"),
            {"gid": graph_id, "nid": node_id, **ctx}).fetchall()
    finally:
        db.close()
    return {"node": {"id": n[0], "type": n[1], "name": n[2], "source_type": n[3],
                     "source_ref": n[4], "summary": n[5], "language": n[6]},
            "neighbors": [{"type": r[0], "dst": r[1], "name": r[2]} for r in nbrs]}


def _to_graphml(nodes: list, edges: list) -> str:
    import html
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
           '<graph edgedefault="directed">']
    for nd in nodes:
        out.append(f'<node id="{html.escape(str(nd["id"]))}">'
                   f'<data key="name">{html.escape(str(nd["name"]))}</data></node>')
    for i, e in enumerate(edges):
        out.append(f'<edge id="e{i}" source="{html.escape(str(e["src"]))}" '
                   f'target="{html.escape(str(e["dst"]))}"/>')
    out.append('</graph></graphml>')
    return "\n".join(out)
