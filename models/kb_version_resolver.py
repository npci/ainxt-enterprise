# SPDX-License-Identifier: Apache-2.0
"""
Version scope cascade — resolves the right spec version for a query.

Order of precedence (§Phase 4):
  1. explicit version       — caller passed spec_version="v3" → use exactly that.
  2. incident / as-of date  — caller passed as_of="2025-04-12" → find the version
                              whose validity window covered that date.
  3. active version         — fall back to the doc currently authoritative
                              (valid_to IS NULL AND status=APPROVED).

The resolver is product+domain-scoped: a query for product=Rupay/domain=Tech never
returns a UPI spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.logger import logger


@dataclass
class ResolvedVersion:
    doc_id: str
    spec_version: Optional[str]
    name: str
    valid_from: Optional[datetime]
    valid_to: Optional[datetime]
    status: str
    source: str    # "explicit" | "as_of" | "active"


def resolve(
    product_id: str,
    domain: Optional[str] = None,
    spec_version: Optional[str] = None,
    as_of: Optional[datetime] = None,
) -> Optional[ResolvedVersion]:
    """
    Return the single best-matching KnowledgeDocument for the requested scope,
    or None when nothing applies.
    """
    if not product_id:
        return None

    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        from sqlalchemy import or_, and_

        db = SessionLocal()
        try:
            q_base = db.query(KnowledgeDocument).filter(
                KnowledgeDocument.product_id == product_id,
            )
            if domain:
                q_base = q_base.filter(KnowledgeDocument.domain == domain)

            # 1. Explicit version wins outright (even DEPRECATED — the caller asked
            # for it by name; deprecate excludes from default scope, not from
            # explicit lookup).
            if spec_version:
                row = (
                    q_base.filter(KnowledgeDocument.spec_version == spec_version)
                    .filter(KnowledgeDocument.status.in_(["APPROVED", "AUTO_APPROVED", "DEPRECATED"]))
                    .order_by(KnowledgeDocument.valid_from.desc().nullslast())
                    .first()
                )
                if row:
                    return _wrap(row, source="explicit")

            # 2. As-of timestamp — find the version whose [valid_from, valid_to)
            # window contains the requested moment.
            if as_of:
                row = (
                    q_base.filter(KnowledgeDocument.status.in_(["APPROVED", "AUTO_APPROVED", "DEPRECATED"]))
                    .filter(
                        and_(
                            or_(KnowledgeDocument.valid_from <= as_of, KnowledgeDocument.valid_from.is_(None)),
                            or_(KnowledgeDocument.valid_to > as_of,   KnowledgeDocument.valid_to.is_(None)),
                        )
                    )
                    .order_by(KnowledgeDocument.valid_from.desc().nullslast())
                    .first()
                )
                if row:
                    return _wrap(row, source="as_of")

            # 3. Active version: APPROVED + valid_to IS NULL.
            row = (
                q_base.filter(KnowledgeDocument.status.in_(["APPROVED", "AUTO_APPROVED"]))
                .filter(KnowledgeDocument.valid_to.is_(None))
                .order_by(KnowledgeDocument.valid_from.desc().nullslast())
                .first()
            )
            if row:
                return _wrap(row, source="active")
            return None
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"kb_version_resolver.resolve failed: {e}")
        return None


def lineage(doc_id: str, max_depth: int = 20) -> list[ResolvedVersion]:
    """
    Walk parent_doc_id pointers backwards from `doc_id`. Useful for version-diff
    queries and for the version-history UI.
    """
    out: list[ResolvedVersion] = []
    if not doc_id:
        return out
    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument

        db = SessionLocal()
        try:
            current = db.get(KnowledgeDocument, doc_id)
            depth = 0
            while current and depth < max_depth:
                out.append(_wrap(current, source="lineage"))
                if not current.parent_doc_id:
                    break
                current = db.get(KnowledgeDocument, current.parent_doc_id)
                depth += 1
            return out
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"kb_version_resolver.lineage failed: {e}")
        return out


def diff_versions(prev_doc_id: str, next_doc_id: str) -> dict:
    """
    Return a high-level diff between two doc versions, driven by section_path
    rather than raw line diff (which would be noisy on prose). The reasoner can
    follow up with a deeper diff on a specific section as needed.
    """
    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        from store.kb_doc_cache import get_or_warm as _warm

        db = SessionLocal()
        try:
            prev = db.get(KnowledgeDocument, prev_doc_id)
            nxt  = db.get(KnowledgeDocument, next_doc_id)
            if not prev or not nxt:
                return {"error": "doc not found"}
            prev_payload = _warm(str(prev.product_id) if prev.product_id else None,
                                 prev.spec_version, str(prev.id))
            next_payload = _warm(str(nxt.product_id) if nxt.product_id else None,
                                 nxt.spec_version, str(nxt.id))
        finally:
            db.close()

        if not prev_payload or not next_payload:
            return {"error": "doc cache miss"}

        prev_sections = {s["section_path"]: s for s in prev_payload.get("section_map", [])}
        next_sections = {s["section_path"]: s for s in next_payload.get("section_map", [])}

        added   = sorted(set(next_sections) - set(prev_sections))
        removed = sorted(set(prev_sections) - set(next_sections))
        changed = []
        for path, n_entry in next_sections.items():
            if path not in prev_sections:
                continue
            p_entry = prev_sections[path]
            p_body = (prev_payload.get("full_md") or "")[p_entry["start"]:p_entry["end"]]
            n_body = (next_payload.get("full_md") or "")[n_entry["start"]:n_entry["end"]]
            if p_body.strip() != n_body.strip():
                changed.append(path)
        return {
            "prev_doc_id": prev_doc_id,
            "next_doc_id": next_doc_id,
            "added":       added,
            "removed":     removed,
            "changed":     changed,
        }
    except Exception as e:
        logger.warning(f"kb_version_resolver.diff_versions failed: {e}")
        return {"error": str(e)}


def _wrap(row, source: str) -> ResolvedVersion:
    return ResolvedVersion(
        doc_id       = str(row.id),
        spec_version = row.spec_version,
        name         = row.name or row.filename,
        valid_from   = row.valid_from,
        valid_to     = row.valid_to,
        status       = row.status,
        source       = source,
    )
