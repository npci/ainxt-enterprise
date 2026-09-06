# SPDX-License-Identifier: MIT
"""
KB entity-extraction worker (Phase 5).

Runs on the index_queue out-of-band, AFTER activate_doc has indexed a spec.
For each leaf chunk we:

  1. Apply a deterministic capitalized-NP extractor to pull candidate entities.
  2. Optionally run the in-house LLM via the LLM proxy to enrich the candidate
     list with relations (controlled by KB_ENTITY_LLM_ENABLED, default false —
     deterministic-only is the safe default).
  3. Resolve each candidate through the Canonical Entity Registry (product-scoped).
  4. Write entity edges (`kb_edges` edge_type='entity') so the retriever can do
     multi-hop discovery without overriding verbatim source text (§Phase 5 keystone).

Idempotency: a content-hash dedup table-style guard is implemented via INSERT
ON CONFLICT DO NOTHING semantics — re-running on the same doc is a no-op.
"""

from __future__ import annotations

import os
import re

from core.logger import logger


_CAP_NP_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9]{2,}(?:\s+[A-Z][A-Za-z0-9]+){0,4})\b"
)
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,8}(?:-[A-Z0-9]{1,6})?\b")

# Common English noise — never promote these to entities.
_NOISE = {
    "this", "that", "these", "those", "the", "and", "for", "with",
    "however", "therefore", "section", "page", "table", "figure",
    "note", "example", "above", "below",
}

# ── Part U14 (2026-06-08) — relation extractor patterns ──────────────────
# docx §10 lineage chain (TPMC approves → SPEC implements → RBI governs →
# BRD references). Each pattern captures (verb_phrase, target_text). The
# target_text is then resolved through kb_entity_registry; on a hit we
# write a dependency edge whose props.relation is the kind below.
#
# Patterns are conservative — they require an uppercase initial on the
# target to avoid matching prose ("approved by the team" must NOT create
# an edge, but "approved by TPMC Decision 2024-08" must). The target
# capture group continues across uppercase words and known number/dash/
# slash characters seen in AiNxt references (e.g. "RBI/2024-25/12").
#
# Each entry: relation_kind → list of (trigger_regex, target_group_idx).
_RELATION_PATTERNS: dict[str, list[tuple[re.Pattern, int]]] = {
    "approved_by": [
        (re.compile(
            r"\b(?:approved\s+by|approval\s+from|sanctioned\s+by)\s+"
            r"((?:[A-Z][A-Za-z0-9._/\-]+\s*){1,6})",
            re.IGNORECASE
        ), 1),
    ],
    "implements": [
        (re.compile(
            r"\b(?:implements?|implementing|in\s+compliance\s+with)\s+"
            r"(?:the\s+)?((?:[A-Z][A-Za-z0-9._/\-]+\s*){1,6})",
            re.IGNORECASE
        ), 1),
    ],
    "governed_by": [
        (re.compile(
            r"\b(?:governed\s+by|per|as\s+per|in\s+accordance\s+with)\s+"
            r"((?:RBI|AiNxt)\s+(?:circular|directive|notification|guideline)\s+"
            r"[A-Za-z0-9._/\-]+)",
            re.IGNORECASE
        ), 1),
    ],
    "references": [
        (re.compile(
            r"\b(?:refers?\s+to|references?|see\s+also|as\s+described\s+in)\s+"
            r"(?:the\s+)?((?:[A-Z][A-Za-z0-9._/\-]+\s*){1,6})",
            re.IGNORECASE
        ), 1),
    ],
    "supersedes": [
        (re.compile(
            r"\b(?:supersedes?|replaces?|deprecates?)\s+"
            r"(?:the\s+)?((?:[A-Z][A-Za-z0-9._/\-]+\s*){1,6})",
            re.IGNORECASE
        ), 1),
    ],
}


def _scan_relations(text: str) -> list[tuple[str, str, str]]:
    """
    Scan `text` for relation cues. Returns a list of
    (relation_kind, target_surface, evidence_sentence) tuples.

    Conservative — only the first match per (relation_kind, target) is kept
    per chunk so a chatty paragraph doesn't create N duplicate edges.
    """
    if not text:
        return []
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, patterns in _RELATION_PATTERNS.items():
        for pat, target_idx in patterns:
            for m in pat.finditer(text):
                target = (m.group(target_idx) or "").strip().rstrip(".,;:")
                if not target or len(target) < 3:
                    continue
                # Skip noise targets the entity extractor would also drop.
                _first_word = target.split()[0].lower()
                if _first_word in _NOISE:
                    continue
                key = (kind, target.lower())
                if key in seen:
                    continue
                seen.add(key)
                # Capture the sentence the trigger appears in as evidence.
                _s = max(0, m.start() - 120)
                _e = min(len(text), m.end() + 120)
                evidence = text[_s:_e].strip()
                out.append((kind, target, evidence))
    return out


def _edge_exists(
        src_chunk_id: str,
        relation: str,
        src_entity_id: str | None,
        dst_entity_id: str | None,
) -> bool:
    """
    Dedupe guard for the relation extractor — checks whether a dependency
    edge with the same (src_chunk_id, relation, src_entity_id, dst_entity_id)
    already exists. Cheap with idx_kb_edges_relation (Part U14 functional
    index). Conservative: any error returns False so the insert still tries
    and the DB layer surfaces the real failure (it shouldn't dedupe-collide
    given the keys above).
    """
    try:
        from db.database import VectorSessionLocal
        from sqlalchemy import text as _sql
        _db = VectorSessionLocal()
        try:
            row = _db.execute(_sql("""
                SELECT 1 FROM kb_edges
                WHERE edge_type = 'dependency'
                  AND src_chunk_id = :sc
                  AND (props->>'relation') = :rel
                  AND COALESCE(src_entity_id::text, '') = COALESCE(:se, '')
                  AND COALESCE(dst_entity_id::text, '') = COALESCE(:de, '')
                LIMIT 1
            """), {"sc": src_chunk_id, "rel": relation,
                   "se": src_entity_id, "de": dst_entity_id}).fetchone()
            return bool(row)
        finally:
            _db.close()
    except Exception:
        return False


def _candidate_phrases(text: str) -> set[str]:
    """Cheap deterministic NER — capitalized noun phrases + ALL-CAPS acronyms."""
    cands: set[str] = set()
    for m in _CAP_NP_RE.finditer(text or ""):
        phrase = m.group(0).strip()
        if phrase.lower() in _NOISE:
            continue
        if len(phrase) < 3:
            continue
        cands.add(phrase)
    for m in _ACRONYM_RE.finditer(text or ""):
        a = m.group(0).strip()
        if a.lower() in _NOISE or len(a) < 2:
            continue
        cands.add(a)
    return cands


def extract_doc(doc_id: str) -> dict:
    """
    Walk every leaf chunk for `doc_id` and write entity edges.

    Returns a summary dict: { "entities": N, "edges": M, "chunks": K }.
    """
    try:
        from db.database import VectorSessionLocal
        from sqlalchemy import text as _sql
        from store import kb_entity_registry as _reg

        vdb = VectorSessionLocal()
        try:
            rows = vdb.execute(_sql("""
                SELECT id, content, product_id, spec_version
                FROM document_embeddings
                WHERE metadata->>'doc_id' = :did
                  AND is_section_parent = FALSE
                ORDER BY chunk_index
            """), {"did": doc_id}).fetchall()
        finally:
            vdb.close()

        if not rows:
            logger.info(f"kb_entity_worker: doc {doc_id} has no leaves — skip")
            return {"entities": 0, "edges": 0, "chunks": 0}

        # First pass: resolve all entities in product scope, accumulating
        # (chunk_id, entity_id) pairs so we can write edges in bulk.
        entity_count   = 0
        edge_count     = 0
        chunk_count    = 0
        # Cluster entities seen in the same chunk so we get co-occurrence edges.
        for r in rows:
            chunk_id    = str(r[0])
            content     = r[1] or ""
            product_id  = str(r[2]) if r[2] else None
            spec_ver    = r[3]
            chunk_count += 1
            if not product_id:
                # Spec-only feature — KB docs without a product scope skip.
                continue
            cands = _candidate_phrases(content)
            if not cands:
                continue
            resolved_ids: list[str] = []
            for surface in cands:
                ent = _reg.resolve_entity(
                    surface_form=surface,
                    product_id=product_id,
                    create_if_missing=True,
                )
                if ent and ent.get("id"):
                    resolved_ids.append(ent["id"])
                    entity_count += 1
            # Pair-wise co-occurrence edges (cheap — capped to avoid quadratic blow-up).
            uniq = list(dict.fromkeys(resolved_ids))[:24]
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    _reg.link_chunks(
                        edge_type="entity",
                        src_doc_id=doc_id,
                        src_chunk_id=chunk_id,
                        dst_doc_id=doc_id,
                        dst_chunk_id=chunk_id,
                        product_id=product_id,
                        spec_version=spec_ver,
                        src_entity_id=uniq[i],
                        dst_entity_id=uniq[j],
                        props={"relation": "co_occurs"},
                    )
                    edge_count += 1

            # ── Part U14 (docx §10) — typed dependency edges ────────────────
            # Second pass: scan the chunk for relation cues (approved_by,
            # implements, governed_by, references, supersedes). For each
            # match, resolve the target through the Canonical Entity Registry
            # and write a dependency edge whose props.relation pins the kind.
            # Idempotent: _edge_exists is checked before the INSERT (cheap with
            # the U14 functional index on (edge_type, props->>'relation')).
            for kind, target_surface, evidence in _scan_relations(content):
                _target_ent = _reg.resolve_entity(
                    surface_form=target_surface,
                    product_id=product_id,
                    create_if_missing=True,
                )
                _target_eid = (_target_ent or {}).get("id")
                if not _target_eid:
                    continue
                # src_entity_id = first co-occurring entity in the chunk (the
                # one most likely to be the chunk's "subject"). When no entity
                # was extracted, leave src NULL — the edge still links docs.
                _src_eid = uniq[0] if uniq else None
                if _edge_exists(chunk_id, kind, _src_eid, _target_eid):
                    continue
                _reg.link_chunks(
                    edge_type="dependency",
                    src_doc_id=doc_id,
                    src_chunk_id=chunk_id,
                    dst_doc_id=None,  # target doc resolved via dst_entity_id traversal
                    dst_chunk_id=None,
                    product_id=product_id,
                    spec_version=spec_ver,
                    src_entity_id=_src_eid,
                    dst_entity_id=_target_eid,
                    props={"relation": kind, "evidence": evidence[:400]},
                )
                edge_count += 1

        # Optional LLM enrichment — gated, defaults off. The LLM runs locally
        # via the proxy and is allowed to TAG additional relations only; it
        # never compresses content (§8z).
        if os.getenv("KB_ENTITY_LLM_ENABLED", "false").lower() in ("1", "true", "yes"):
            _llm_enrich(doc_id)

        logger.info(
            f"kb_entity_worker: doc {doc_id} done — "
            f"chunks={chunk_count} entities={entity_count} edges={edge_count}"
        )
        return {"entities": entity_count, "edges": edge_count, "chunks": chunk_count}
    except Exception as e:
        logger.error(f"kb_entity_worker.extract_doc failed: {e}")
        return {"error": str(e)}


def _llm_enrich(doc_id: str) -> None:
    """Optional second-pass enrichment via the in-house LLM proxy. Best-effort."""
    try:
        from core.config import LLM_PROXY_URL  # type: ignore[attr-defined]
    except Exception:
        LLM_PROXY_URL = os.getenv("LLM_PROXY_URL", "")
    if not LLM_PROXY_URL:
        return
    logger.info(f"kb_entity_worker: LLM enrichment hook reached for {doc_id} (no-op stub)")


def enqueue(doc_id: str) -> None:
    """Convenience wrapper called from docs_store.activate_doc."""
    try:
        from core.job_queue import get_queue
        q = get_queue("index_queue")
        q.enqueue(
            "workers.kb_entity_worker.extract_doc",
            doc_id,
            job_timeout=3600,
            result_ttl=600,
        )
    except Exception as e:
        logger.warning(f"kb_entity_worker.enqueue failed: {e}")
