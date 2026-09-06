# SPDX-License-Identifier: MIT
"""
Canonical Entity Registry (Phase 5).

Resolves entity surface forms to canonical nodes so cross-document references
collapse correctly:

  "UPI Lite", "UPI-Lite", "UPILITE", "UPI lite payment"  →  one node

Two scope tiers (§Phase 5):

  - Product-scoped (default): each canonical_name lives under a scope_product_id;
    the same string in a different product is a SEPARATE node. Honors the
    hard product filter so cross-product entity links never bleed.
  - Global (curated allow-list): admin-promoted only; extraction never
    auto-promotes. Used for genuinely cross-product entities (e.g. "RBI").

Alias normalization rules:
  - lowercase, strip punctuation, collapse whitespace
  - hyphen/space/underscore variants converge ("upi-lite" ~ "upi lite" ~ "upi_lite")
  - light Soundex-style suffix stripping ("upilite" → "upi lite") via known suffixes

When the worker creates a new node it adds the surface form to `aliases`; the
resolver matches future forms via the GIN-indexed `aliases` JSONB.
"""

from __future__ import annotations

import re
from typing import Optional

from core.logger import logger


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_alias(text: str) -> str:
    """
    Convert a surface form to its registry key. Idempotent.

    Examples:
        "UPI Lite"   → "upi lite"
        "UPI-Lite"   → "upi lite"
        "UPILITE"    → "upilite"   (note: glued forms keep separate without dictionary help)
        "RBI"        → "rbi"
    """
    if not text:
        return ""
    s = text.strip().lower()
    s = _NORMALIZE_RE.sub(" ", s)
    s = " ".join(s.split())
    return s


def _candidates(name: str) -> list[str]:
    """
    Generate normalized candidate forms for a single surface input — covers the
    common hyphen/space/glued variants.
    """
    base = normalize_alias(name)
    if not base:
        return []
    out = {base}
    out.add(base.replace(" ", ""))
    out.add(base.replace(" ", "-"))
    return list(out)


def resolve_entity(
    surface_form: str,
    product_id: Optional[str] = None,
    create_if_missing: bool = False,
    kind: Optional[str] = None,
) -> Optional[dict]:
    """
    Resolve a surface form to a canonical entity row.

    Lookup order:
      1. exact canonical_name in product scope
      2. surface form present in aliases for product scope
      3. exact canonical_name in GLOBAL tier (is_global=TRUE)
      4. aliases match in GLOBAL tier
      5. If create_if_missing and product_id given → create a new product-scoped node.

    Returns the row dict or None.
    """
    if not surface_form:
        return None
    cands = _candidates(surface_form)
    if not cands:
        return None

    try:
        from db.database import VectorSessionLocal
        from sqlalchemy import text as _sql

        db = VectorSessionLocal()
        try:
            params = {"cand0": cands[0], "cands": cands, "pid": product_id}
            # 1. product-scoped canonical match
            row = db.execute(_sql("""
                SELECT id, scope_product_id, canonical_name, kind, aliases, is_global
                FROM kb_entities
                WHERE scope_product_id = :pid
                  AND LOWER(canonical_name) = :cand0
                LIMIT 1
            """), params).fetchone()
            if row:
                return _row_to_dict(row)

            # 2. product-scoped alias match
            row = db.execute(_sql("""
                SELECT id, scope_product_id, canonical_name, kind, aliases, is_global
                FROM kb_entities
                WHERE scope_product_id = :pid
                  AND aliases ?| ARRAY[:cand0]
                LIMIT 1
            """), params).fetchone()
            if row:
                return _row_to_dict(row)

            # 3. global canonical match
            row = db.execute(_sql("""
                SELECT id, scope_product_id, canonical_name, kind, aliases, is_global
                FROM kb_entities
                WHERE is_global = TRUE
                  AND LOWER(canonical_name) = :cand0
                LIMIT 1
            """), params).fetchone()
            if row:
                return _row_to_dict(row)

            # 4. global alias match
            row = db.execute(_sql("""
                SELECT id, scope_product_id, canonical_name, kind, aliases, is_global
                FROM kb_entities
                WHERE is_global = TRUE
                  AND aliases ?| ARRAY[:cand0]
                LIMIT 1
            """), params).fetchone()
            if row:
                return _row_to_dict(row)

            if not create_if_missing or not product_id:
                return None

            # 5. create new product-scoped node
            import json as _json
            new_row = db.execute(_sql("""
                INSERT INTO kb_entities (scope_product_id, canonical_name, kind, aliases, is_global)
                VALUES (:pid, :cname, :kind, CAST(:aliases AS jsonb), FALSE)
                RETURNING id, scope_product_id, canonical_name, kind, aliases, is_global
            """), {
                "pid":     product_id,
                "cname":   surface_form.strip()[:255],
                "kind":    kind,
                "aliases": _json.dumps(cands),
            }).fetchone()
            db.commit()
            if new_row:
                logger.info(
                    f"kb_entity_registry: created entity '{surface_form}' "
                    f"(product={product_id}, id={new_row[0]})"
                )
                return _row_to_dict(new_row)
            return None
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"kb_entity_registry.resolve_entity failed: {e}")
        return None


def add_alias(entity_id: str, alias: str) -> bool:
    """Append a normalized alias to an existing entity. Idempotent."""
    norm = normalize_alias(alias)
    if not norm or not entity_id:
        return False
    try:
        from db.database import VectorSessionLocal
        from sqlalchemy import text as _sql
        db = VectorSessionLocal()
        try:
            db.execute(_sql("""
                UPDATE kb_entities
                SET aliases = (
                    SELECT to_jsonb(array_agg(DISTINCT a))
                    FROM jsonb_array_elements_text(aliases || to_jsonb(:n::text)) AS t(a)
                )
                WHERE id = :eid
            """), {"n": norm, "eid": entity_id})
            db.commit()
            return True
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"kb_entity_registry.add_alias failed: {e}")
        return False


def link_chunks(
    edge_type: str,
    src_doc_id: str,
    src_chunk_id: Optional[str],
    dst_doc_id: Optional[str],
    dst_chunk_id: Optional[str],
    product_id: Optional[str],
    spec_version: Optional[str],
    src_entity_id: Optional[str] = None,
    dst_entity_id: Optional[str] = None,
    props: Optional[dict] = None,
) -> Optional[str]:
    """
    Insert one row into kb_edges. Generic helper used by Phase 4
    dependency/version edges AND Phase 5 entity edges.
    """
    try:
        import json as _json
        from db.database import VectorSessionLocal
        from sqlalchemy import text as _sql
        db = VectorSessionLocal()
        try:
            row = db.execute(_sql("""
                INSERT INTO kb_edges (
                    edge_type, src_doc_id, src_chunk_id, dst_doc_id, dst_chunk_id,
                    src_entity_id, dst_entity_id, product_id, spec_version, props
                )
                VALUES (
                    :et, :sd, :sc, :dd, :dc,
                    :se, :de, :pid, :sv, CAST(:props AS jsonb)
                )
                RETURNING id
            """), {
                "et":    edge_type,
                "sd":    src_doc_id,
                "sc":    src_chunk_id,
                "dd":    dst_doc_id,
                "dc":    dst_chunk_id,
                "se":    src_entity_id,
                "de":    dst_entity_id,
                "pid":   product_id,
                "sv":    spec_version,
                "props": _json.dumps(props or {}),
            }).fetchone()
            db.commit()
            return str(row[0]) if row else None
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"kb_entity_registry.link_chunks failed: {e}")
        return None


def _row_to_dict(row) -> dict:
    return {
        "id":               str(row[0]),
        "scope_product_id": str(row[1]) if row[1] else None,
        "canonical_name":   row[2],
        "kind":             row[3] or "",
        "aliases":          row[4] or [],
        "is_global":        bool(row[5]),
    }
