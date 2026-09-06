# SPDX-License-Identifier: MIT
# ============================================================
# KNOWLEDGE GRAPH WORKER (RQ — kb_queue)
#
# Builds the unified knowledge graph (code + docs):
#   - extract_doc_entities_job : LLM entity/relation extraction over KB doc chunks
#   - build_graph_job          : per-graph_id dispatcher (repo: / kb: / cross:)
#   - cluster_domains_job      : LLM domain clustering + networkx centrality (P5)
#   - build_cross_links_job    : code <-> docs name cross-linking (P5)
#   - delete_doc_nodes         : purge a doc's nodes (called from the doc delete path)
#
# Code nodes are mirrored at index time by workers/index_worker._mirror_code_nodes_to_kg.
# RBAC columns mirror document_embeddings so graph queries enforce access scoping.
# All upserts are idempotent; jobs are best-effort and never crash the queue worker.
# ============================================================

from __future__ import annotations

import hashlib
import json
import re

from core.logger import logger

# ── DB helpers (PGS01) ──────────────────────────────────────────────────────

def _db():
    from db.database import SessionLocal
    return SessionLocal()


def _vector_db():
    from db.database import VectorSessionLocal
    return VectorSessionLocal()


_NODE_SQL = """
    INSERT INTO knowledge_graph_nodes
        (graph_id, node_id, node_type, name, source_type, source_ref, language,
         summary, classification, department, min_band_level, visibility)
    VALUES
        (:graph_id, :node_id, :node_type, :name, :source_type, :source_ref, :language,
         :summary, :classification, :department, :min_band_level, :visibility)
    ON CONFLICT (graph_id, node_id) DO UPDATE SET
        node_type      = EXCLUDED.node_type,
        name           = EXCLUDED.name,
        summary        = EXCLUDED.summary,
        source_ref     = EXCLUDED.source_ref,
        classification = EXCLUDED.classification,
        department     = EXCLUDED.department,
        updated_at     = NOW()
"""

_EDGE_SQL = """
    INSERT INTO knowledge_graph_edges
        (graph_id, src_node_id, dst_node_id, edge_type, classification, min_band_level)
    VALUES (:graph_id, :src_node_id, :dst_node_id, :edge_type, :classification, :min_band_level)
    ON CONFLICT (graph_id, src_node_id, dst_node_id, edge_type) DO NOTHING
"""


def _upsert_nodes(db, rows: list[dict]) -> None:
    from sqlalchemy import text as _sql
    if not rows:
        return
    stmt = _sql(_NODE_SQL)
    for i in range(0, len(rows), 500):
        db.execute(stmt, rows[i:i + 500])


def _upsert_edges(db, rows: list[dict]) -> None:
    from sqlalchemy import text as _sql
    if not rows:
        return
    stmt = _sql(_EDGE_SQL)
    for i in range(0, len(rows), 500):
        db.execute(stmt, rows[i:i + 500])


def _set_status(db, graph_id: str, **fields) -> None:
    """Upsert knowledge_graph_build_status for graph_id (partial fields)."""
    from sqlalchemy import text as _sql
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(f":{k}" for k in fields)
    updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields)
    sql = _sql(f"""
        INSERT INTO knowledge_graph_build_status (graph_id, {cols}, updated_at)
        VALUES (:graph_id, {placeholders}, NOW())
        ON CONFLICT (graph_id) DO UPDATE SET {updates}, updated_at = NOW()
    """)
    db.execute(sql, {"graph_id": graph_id, **fields})


# ── LLM extraction ──────────────────────────────────────────────────────────

_ENTITY_PROMPT = (
    "Extract a knowledge graph from the text below. Return ONLY a JSON object "
    'with exactly two keys:\n'
    '  "entities": [{"name": str, "type": "concept|system|person|process|policy|metric|domain", "summary": str (<=40 words)}]\n'
    '  "relations": [{"src": str, "dst": str, "type": "mentions|related_to|defines|uses|governed_by|part_of"}]\n'
    "Only include named entities specific to this document's domain. Use the exact "
    "entity names in relations. No prose, no markdown — JSON only.\n\nTEXT:\n"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_MAX_LLM_CALLS = 5
_MAX_ENTITIES = 50
_MAX_RELATIONS = 100
_MIN_DOC_CHARS = 500


def _llm_extract(chunk_text: str) -> dict | None:
    """One LLM extraction call. simple tier first; escalate to complex once on bad JSON."""
    from models.model_router import model_router
    prompt = _ENTITY_PROMPT + chunk_text[:1500]
    for hint in ("simple", "complex"):
        try:
            raw = model_router.generate(prompt, model_hint=hint) or ""
        except Exception as e:
            logger.warning(f"kg_worker: LLM ({hint}) error: {e}")
            continue
        m = _JSON_RE.search(raw)
        if not m:
            continue
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue  # malformed → try the next (complex) tier
    return None


def extract_doc_entities_job(payload: dict) -> dict:
    """
    Extract entities + relations from a KB document's chunks into the unified graph.

    payload = {namespace, doc_id, doc_title, chunk_texts?, rbac:{classification,department}, triggered_by}
    If chunk_texts is absent (manual rebuild), chunks are fetched from document_embeddings.
    Content-hash gated: a doc whose chunk set is unchanged is a no-op.
    """
    namespace = payload.get("namespace") or ""
    doc_id    = payload.get("doc_id") or ""
    title     = payload.get("doc_title") or doc_id
    rbac      = payload.get("rbac") or {}
    if not namespace or not doc_id:
        return {"ok": False, "error": "namespace + doc_id required"}

    graph_id = f"kb:{namespace}"
    chunks = payload.get("chunk_texts") or _fetch_doc_chunks(namespace, doc_id)
    if not chunks:
        return {"ok": False, "error": "no chunks"}
    joined = "\n".join(chunks)
    if len(joined) < _MIN_DOC_CHARS:
        return {"ok": True, "skipped": "doc too small"}

    doc_hash = hashlib.sha256(joined.encode()).hexdigest()[:32]
    if _doc_hash_unchanged(graph_id, doc_id, doc_hash):
        return {"ok": True, "skipped": "unchanged"}

    classification = rbac.get("classification") or "internal"
    department     = rbac.get("department")

    entities: dict[str, dict] = {}   # lower(name) -> {name,type,summary}
    relations: list[dict] = []
    for chunk in chunks[: _MAX_LLM_CALLS]:
        obj = _llm_extract(chunk)
        if not obj:
            continue
        for e in (obj.get("entities") or []):
            nm = (e.get("name") or "").strip()
            if not nm:
                continue
            key = nm.lower()
            if key not in entities and len(entities) < _MAX_ENTITIES:
                entities[key] = {"name": nm, "type": e.get("type") or "concept",
                                 "summary": (e.get("summary") or "")[:300]}
        for r in (obj.get("relations") or []):
            if len(relations) >= _MAX_RELATIONS:
                break
            if r.get("src") and r.get("dst"):
                relations.append(r)

    # ── Build node/edge rows ──────────────────────────────────────────────
    def _nid(name: str) -> str:
        return f"doc_{doc_id}::{name.lower()}"

    node_rows = [{
        "graph_id": graph_id, "node_id": f"doc::{doc_id}", "node_type": "document",
        "name": title, "source_type": "doc", "source_ref": doc_id, "language": None,
        "summary": joined[:200], "classification": classification,
        "department": department, "min_band_level": 0, "visibility": "PUBLIC",
    }]
    for e in entities.values():
        node_rows.append({
            "graph_id": graph_id, "node_id": _nid(e["name"]), "node_type": e["type"],
            "name": e["name"], "source_type": "doc", "source_ref": doc_id, "language": None,
            "summary": e["summary"], "classification": classification,
            "department": department, "min_band_level": 0, "visibility": "PUBLIC",
        })

    edge_rows = []
    # document --mentions--> each entity
    for e in entities.values():
        edge_rows.append({
            "graph_id": graph_id, "src_node_id": f"doc::{doc_id}", "dst_node_id": _nid(e["name"]),
            "edge_type": "mentions", "classification": classification, "min_band_level": 0,
        })
    # entity --rel--> entity (only between known entities)
    for r in relations:
        s, d = r["src"].lower(), r["dst"].lower()
        if s in entities and d in entities:
            edge_rows.append({
                "graph_id": graph_id, "src_node_id": _nid(r["src"]), "dst_node_id": _nid(r["dst"]),
                "edge_type": (r.get("type") or "related_to"),
                "classification": classification, "min_band_level": 0,
            })

    db = _db()
    try:
        _upsert_nodes(db, node_rows)
        _upsert_edges(db, edge_rows)
        _bump_doc_hash(db, graph_id, doc_id, doc_hash, len(node_rows))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"kg_worker: extract_doc_entities_job failed: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        db.close()
    logger.info(f"kg_worker: extracted {len(entities)} entities / {len(edge_rows)} edges for doc {doc_id} ({graph_id})")
    return {"ok": True, "entities": len(entities), "edges": len(edge_rows)}


def _fetch_doc_chunks(namespace: str, doc_id: str, limit: int = 30) -> list[str]:
    """Read chunk content from pgvector for a doc (used on manual rebuild after chunks cleared)."""
    from sqlalchemy import text as _sql
    repo = f"docs_kb:{namespace.lower()}"
    vdb = _vector_db()
    try:
        rows = vdb.execute(_sql(
            "SELECT content FROM document_embeddings "
            "WHERE repo = :repo AND metadata->>'doc_id' = :doc_id "
            "ORDER BY chunk_index LIMIT :lim"
        ), {"repo": repo, "doc_id": doc_id, "lim": limit}).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception as e:
        logger.warning(f"kg_worker: fetch chunks failed for {doc_id}: {e}")
        return []
    finally:
        vdb.close()


def _doc_hash_unchanged(graph_id: str, doc_id: str, doc_hash: str) -> bool:
    from sqlalchemy import text as _sql
    db = _db()
    try:
        row = db.execute(_sql(
            "SELECT metadata->>:k FROM knowledge_graph_build_status WHERE graph_id = :gid"
        ), {"k": f"doc_hash_{doc_id}", "gid": graph_id}).fetchone()
        return bool(row and row[0] == doc_hash)
    except Exception:
        return False
    finally:
        db.close()


def _bump_doc_hash(db, graph_id: str, doc_id: str, doc_hash: str, node_delta: int) -> None:
    from sqlalchemy import text as _sql
    db.execute(_sql("""
        INSERT INTO knowledge_graph_build_status (graph_id, status, doc_nodes, metadata, last_built_at, updated_at)
        VALUES (:gid, 'done', :nd, jsonb_build_object(:hk, :hv), NOW(), NOW())
        ON CONFLICT (graph_id) DO UPDATE SET
            status        = 'done',
            doc_nodes     = knowledge_graph_build_status.doc_nodes + :nd,
            metadata      = knowledge_graph_build_status.metadata || jsonb_build_object(:hk, :hv),
            last_built_at = NOW(),
            updated_at    = NOW()
    """), {"gid": graph_id, "nd": node_delta, "hk": f"doc_hash_{doc_id}", "hv": doc_hash})


def delete_doc_nodes(doc_id: str, namespace: str | None = None) -> dict:
    """Purge a document's nodes + their edges from the graph (called on doc delete)."""
    from sqlalchemy import text as _sql
    if not doc_id:
        return {"ok": False, "error": "doc_id required"}
    gid = f"kb:{namespace}" if namespace else None
    db = _db()
    try:
        node_pat = f"doc_{doc_id}::%"
        doc_node = f"doc::{doc_id}"
        params = {"np": node_pat, "dn": doc_node}
        gfilter = "graph_id = :gid AND " if gid else ""
        if gid:
            params["gid"] = gid
        # edges first (src or dst belongs to this doc)
        db.execute(_sql(
            f"DELETE FROM knowledge_graph_edges WHERE {gfilter}"
            "(src_node_id LIKE :np OR dst_node_id LIKE :np OR src_node_id = :dn OR dst_node_id = :dn)"
        ), params)
        db.execute(_sql(
            f"DELETE FROM knowledge_graph_nodes WHERE {gfilter}(node_id LIKE :np OR node_id = :dn)"
        ), params)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"kg_worker: delete_doc_nodes failed for {doc_id}: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        db.close()
    return {"ok": True}


def build_graph_job(payload: dict) -> dict:
    """
    Per-graph_id build dispatcher (enqueued by POST /graph/build).
      repo:<name>   — code mirror is automatic at index time; mark status done.
      kb:<ns>       — (re)extract all approved docs in the namespace.
      cross:<a>:<b> — cross-link code <-> docs (see build_cross_links_job, P5).
    """
    graph_id = payload.get("graph_id") or ""
    db = _db()
    try:
        _set_status(db, graph_id, status="running")
        db.commit()
    finally:
        db.close()

    from core.job_queue import enqueue_job, Q_KB
    trigger_domain = bool(payload.get("trigger_domain"))
    trigger_cross = payload.get("trigger_cross")   # e.g. "kb:payments" when graph_id is "repo:X"
    try:
        if graph_id.startswith("kb:"):
            result = {"ok": True, "graph_id": graph_id,
                      "docs_enqueued": _rebuild_kb_namespace(graph_id[3:])}
        else:
            # repo:<name> — code mirror happens during indexing; confirm status
            db = _db()
            try:
                _set_status(db, graph_id, status="done")
                db.commit()
            finally:
                db.close()
            result = {"ok": True, "graph_id": graph_id,
                      "note": "code graph is built during repo indexing"}

        # ── Follow-on jobs ────────────────────────────────────────────────
        if trigger_cross and graph_id.startswith("repo:"):
            try:
                enqueue_job("workers.knowledge_graph_worker.build_cross_links_job",
                            {"repo_graph": graph_id, "kb_graph": trigger_cross},
                            queue_name=Q_KB, timeout=900, retry_count=1)
            except Exception as _ce:
                logger.warning(f"kg_worker: cross-link enqueue failed: {_ce}")
        if trigger_domain:
            try:
                enqueue_job("workers.knowledge_graph_worker.cluster_domains_job",
                            {"graph_id": graph_id}, queue_name=Q_KB, timeout=900, retry_count=1)
            except Exception as _de:
                logger.warning(f"kg_worker: domain enqueue failed: {_de}")
        return result
    except Exception as e:
        logger.error(f"kg_worker: build_graph_job({graph_id}) failed: {e}")
        db = _db()
        try:
            _set_status(db, graph_id, status="failed", error=str(e)[:500])
            db.commit()
        finally:
            db.close()
        return {"ok": False, "error": str(e)}


def _rebuild_kb_namespace(namespace: str) -> int:
    """Enqueue entity extraction for every APPROVED doc in the namespace."""
    from sqlalchemy import text as _sql
    from core.job_queue import enqueue_job, Q_KB
    db = _db()
    try:
        rows = db.execute(_sql(
            "SELECT id, filename FROM knowledge_docs "
            "WHERE namespace = :ns AND status IN ('APPROVED','AUTO_APPROVED')"
        ), {"ns": namespace}).fetchall()
    finally:
        db.close()
    count = 0
    for r in rows:
        try:
            enqueue_job(
                "workers.knowledge_graph_worker.extract_doc_entities_job",
                {"namespace": namespace, "doc_id": str(r[0]), "doc_title": r[1] or str(r[0]),
                 "rbac": {"classification": "internal", "department": None},
                 "triggered_by": "build"},
                queue_name=Q_KB, timeout=600, retry_count=1,
            )
            count += 1
        except Exception as e:
            logger.warning(f"kg_worker: enqueue extract failed for doc {r[0]}: {e}")
    return count


# ── P5: Domain view + cross-linking ─────────────────────────────────────────

def cluster_domains_job(payload: dict) -> dict:
    """LLM-cluster a graph's nodes into business/technical domains + write pagerank centrality."""
    from sqlalchemy import text as _sql
    graph_id = payload.get("graph_id") or ""
    if not graph_id:
        return {"ok": False, "error": "graph_id required"}

    db = _db()
    try:
        rows = db.execute(_sql(
            "SELECT node_id, name, node_type, COALESCE(summary,'') "
            "FROM knowledge_graph_nodes WHERE graph_id = :gid "
            "ORDER BY (metadata->>'centrality') DESC NULLS LAST LIMIT 500"),
            {"gid": graph_id}).fetchall()
    finally:
        db.close()
    if not rows:
        return {"ok": True, "domains": 0, "note": "no nodes"}

    name_to_id = {r[1].lower(): r[0] for r in rows}
    listing = "\n".join(f"- {r[1]} ({r[2]}): {r[3][:60]}" for r in rows[:300])
    prompt = (
        f"Group these knowledge-graph entities from '{graph_id}' into 5-15 business/technical "
        'domains. Return ONLY a JSON array: '
        '[{"domain_name": str, "description": str (<=25 words), "member_names": [str, ...]}]. '
        "Use the exact entity names.\n\nENTITIES:\n" + listing
    )
    domains = []
    try:
        from models.model_router import model_router
        raw = model_router.generate(prompt, model_hint="complex") or ""
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            domains = json.loads(m.group(0))
    except Exception as e:
        logger.warning(f"kg_worker: domain LLM failed: {e}")

    _write_centrality(graph_id, len(rows))

    if not isinstance(domains, list) or not domains:
        return {"ok": True, "domains": 0, "note": "no domains extracted"}

    db = _db()
    try:
        db.execute(_sql("DELETE FROM knowledge_graph_domains WHERE graph_id = :gid"), {"gid": graph_id})
        n_written = 0
        for d in domains[:20]:
            dn = (d.get("domain_name") or "").strip()
            if not dn:
                continue
            members = [name_to_id[mn.lower()] for mn in (d.get("member_names") or [])
                       if isinstance(mn, str) and mn.lower() in name_to_id]
            db.execute(_sql("""
                INSERT INTO knowledge_graph_domains
                    (graph_id, domain_name, description, member_node_ids, centroid)
                VALUES (:gid, :dn, :desc, CAST(:members AS jsonb), :centroid)
                ON CONFLICT (graph_id, domain_name) DO UPDATE SET
                    description     = EXCLUDED.description,
                    member_node_ids = EXCLUDED.member_node_ids,
                    centroid        = EXCLUDED.centroid
            """), {"gid": graph_id, "dn": dn, "desc": (d.get("description") or "")[:500],
                   "members": json.dumps(members), "centroid": (members[0] if members else None)})
            n_written += 1
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"kg_worker: cluster_domains_job failed: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        db.close()
    logger.info(f"kg_worker: clustered {n_written} domains for {graph_id}")
    return {"ok": True, "domains": n_written}


def _write_centrality(graph_id: str, node_count: int) -> None:
    """Compute pagerank over the graph's edges (in-memory) into node metadata. Skip large graphs."""
    if node_count > 5000:
        return
    from sqlalchemy import text as _sql
    try:
        import networkx as nx
    except Exception:
        return
    db = _db()
    try:
        edges = db.execute(_sql(
            "SELECT src_node_id, dst_node_id FROM knowledge_graph_edges WHERE graph_id = :gid"),
            {"gid": graph_id}).fetchall()
        if not edges:
            return
        g = nx.DiGraph()
        g.add_edges_from((e[0], e[1]) for e in edges)
        pr = nx.pagerank(g, alpha=0.85, max_iter=50)
        for nid, score in pr.items():
            db.execute(_sql(
                "UPDATE knowledge_graph_nodes "
                "SET metadata = metadata || jsonb_build_object('centrality', :c) "
                "WHERE graph_id = :gid AND node_id = :nid"),
                {"c": round(float(score), 6), "gid": graph_id, "nid": nid})
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"kg_worker: centrality failed for {graph_id}: {e}")
    finally:
        db.close()


def build_cross_links_job(payload: dict) -> dict:
    """
    Cross-link a repo's code nodes with a KB namespace's doc entities by exact name.
    Creates lightweight 'cross' concept nodes + cross_ref edges in the REPO graph, so
    explore(repo:X) surfaces the related KB concepts (single-graph traversable).
    payload = {repo_graph: "repo:X", kb_graph: "kb:Y"}
    """
    from sqlalchemy import text as _sql
    repo_graph = payload.get("repo_graph") or ""
    kb_graph = payload.get("kb_graph") or ""
    if not repo_graph.startswith("repo:") or not kb_graph.startswith("kb:"):
        return {"ok": False, "error": "need repo_graph=repo:X + kb_graph=kb:Y"}

    db = _db()
    try:
        code = db.execute(_sql(
            "SELECT lower(name), node_id FROM knowledge_graph_nodes "
            "WHERE graph_id = :g AND source_type = 'code'"), {"g": repo_graph}).fetchall()
        docs = db.execute(_sql(
            "SELECT lower(name), name, COALESCE(summary,'') FROM knowledge_graph_nodes "
            "WHERE graph_id = :g AND source_type = 'doc' AND node_type <> 'document'"),
            {"g": kb_graph}).fetchall()

        code_by_name: dict[str, list] = {}
        for ln, nid in code:
            code_by_name.setdefault(ln, []).append(nid)

        node_rows: list[dict] = []
        edge_rows: list[dict] = []
        for dln, dname, dsum in docs:
            if len(dln) < 4 or dln not in code_by_name:
                continue
            xref_id = f"kbref::{dln}"
            node_rows.append({
                "graph_id": repo_graph, "node_id": xref_id, "node_type": "concept",
                "name": dname, "source_type": "cross", "source_ref": kb_graph,
                "language": None, "summary": f"KB concept: {dsum[:200]}",
                "classification": "internal", "department": None,
                "min_band_level": 0, "visibility": "PUBLIC",
            })
            for code_nid in code_by_name[dln][:10]:
                edge_rows.append({
                    "graph_id": repo_graph, "src_node_id": code_nid, "dst_node_id": xref_id,
                    "edge_type": "cross_ref", "classification": "internal", "min_band_level": 0,
                })

        _upsert_nodes(db, node_rows)
        _upsert_edges(db, edge_rows)
        _set_status(db, repo_graph, cross_edges=len(edge_rows))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"kg_worker: build_cross_links_job failed: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        db.close()
    logger.info(f"kg_worker: cross-linked {len(node_rows)} concepts / {len(edge_rows)} edges "
                f"({repo_graph} <-> {kb_graph})")
    return {"ok": True, "cross_nodes": len(node_rows), "cross_edges": len(edge_rows)}
