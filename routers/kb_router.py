# SPDX-License-Identifier: Apache-2.0
"""
KB router — version cascade, lineage, diff, coverage trace.

Mounted at /kb (FastAPI). All endpoints are read-only or admin-curated; uploads
continue to flow through routers/docs_router.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth.dependencies import get_current_user
from core.logger import logger


router = APIRouter(prefix="/kb", tags=["knowledge_base"])


@router.get("/resolve")
def resolve_version(
    product_id: str = Query(..., description="Product UUID"),
    domain:     Optional[str] = Query(None),
    spec_version: Optional[str] = Query(None, description="Explicit version e.g. 'v3'"),
    as_of:      Optional[str] = Query(None, description="ISO-8601 timestamp"),
    current_user: dict = Depends(get_current_user),
):
    """
    Resolve a {product, domain, version|as_of} triple to a concrete doc_id via
    the version scope cascade. Returns the chosen ResolvedVersion or 404.
    """
    from models.kb_version_resolver import resolve as _resolve
    as_of_dt = None
    if as_of:
        try:
            as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid as_of timestamp")
    res = _resolve(product_id, domain=domain, spec_version=spec_version, as_of=as_of_dt)
    if not res:
        raise HTTPException(status_code=404, detail="no doc matches the requested scope")
    return {
        "doc_id":       res.doc_id,
        "spec_version": res.spec_version,
        "name":         res.name,
        "status":       res.status,
        "valid_from":   res.valid_from.isoformat() if res.valid_from else None,
        "valid_to":     res.valid_to.isoformat()   if res.valid_to else None,
        "source":       res.source,
    }


@router.get("/lineage/{doc_id}")
def get_lineage(doc_id: str, current_user: dict = Depends(get_current_user)):
    """Return the parent_doc_id chain for `doc_id` (most recent first)."""
    from models.kb_version_resolver import lineage as _lineage
    chain = _lineage(doc_id)
    return {
        "doc_id":  doc_id,
        "lineage": [
            {
                "doc_id":       r.doc_id,
                "spec_version": r.spec_version,
                "name":         r.name,
                "status":       r.status,
                "valid_from":   r.valid_from.isoformat() if r.valid_from else None,
                "valid_to":     r.valid_to.isoformat()   if r.valid_to else None,
            } for r in chain
        ],
    }


@router.get("/diff")
def diff_versions(
    prev_doc_id: str = Query(...),
    next_doc_id: str = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """Section-level diff between two versions of a spec."""
    from models.kb_version_resolver import diff_versions as _diff
    return _diff(prev_doc_id, next_doc_id)


@router.post("/cache/invalidate")
def invalidate_cache(
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """
    Admin-only: drop a doc from the shared {product,version,doc} cache.
    Body: {"doc_id": "..."} or {"product_id": "...", "spec_version": "..."}.
    """
    role = (current_user or {}).get("role")
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    from store import kb_doc_cache as _kbc
    doc_id = (body or {}).get("doc_id")
    product_id = (body or {}).get("product_id")
    spec_version = (body or {}).get("spec_version")
    dropped = 0
    if doc_id:
        dropped += _kbc.invalidate(doc_id)
    if product_id and spec_version:
        dropped += _kbc.invalidate_product_version(product_id, spec_version)
    return {"dropped": dropped}


@router.get("/entities")
def list_entities(
    product_id: Optional[str] = Query(None),
    include_global: bool = Query(True),
    q: Optional[str] = Query(None, description="alias/name substring filter"),
    limit: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    """Browse the canonical entity registry (Phase 5)."""
    try:
        from db.database import VectorReadSessionLocal
        from sqlalchemy import text as _sql
        clauses = []
        params: dict = {"lim": limit}
        if product_id:
            if include_global:
                clauses.append("(scope_product_id = :pid OR is_global = TRUE)")
                params["pid"] = product_id
            else:
                clauses.append("scope_product_id = :pid")
                params["pid"] = product_id
        elif include_global:
            clauses.append("is_global = TRUE")
        if q:
            clauses.append("(LOWER(canonical_name) LIKE :q OR aliases::text ILIKE :q)")
            params["q"] = f"%{q.lower()}%"
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        db = VectorReadSessionLocal()
        try:
            rows = db.execute(_sql(f"""
                SELECT id, scope_product_id, canonical_name, kind, aliases, is_global
                FROM kb_entities
                {where}
                ORDER BY canonical_name
                LIMIT :lim
            """), params).fetchall()
        finally:
            db.close()
        return {
            "entities": [
                {
                    "id":                str(r[0]),
                    "scope_product_id":  str(r[1]) if r[1] else None,
                    "canonical_name":    r[2],
                    "kind":              r[3] or "",
                    "aliases":           r[4] or [],
                    "is_global":         bool(r[5]),
                } for r in rows
            ]
        }
    except Exception as e:
        logger.warning(f"kb_router.list_entities failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/entities/promote")
def promote_entity_global(
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """
    Admin-only: promote a product-scoped entity to the curated GLOBAL tier.
    Body: {"entity_id": "..."}.
    """
    role = (current_user or {}).get("role")
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    entity_id = (body or {}).get("entity_id")
    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id required")
    try:
        from db.database import VectorSessionLocal
        from sqlalchemy import text as _sql
        db = VectorSessionLocal()
        try:
            db.execute(_sql("""
                UPDATE kb_entities
                SET is_global  = TRUE,
                    curated_by = :u,
                    curated_at = NOW()
                WHERE id = :eid
            """), {"u": current_user.get("email", "admin"), "eid": entity_id})
            db.commit()
            return {"success": True}
        finally:
            db.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
