# SPDX-License-Identifier: MIT
"""
Shared {product, version, doc} KB doc cache.

Phase 3 — performance plane (separate from coverage plane).

On first query for a {product, version, doc} triple the worker loads the full
markdown + section map ONCE and parks it in Redis under a shared key. All
2,000 users querying the same spec hit the same cached payload — re-load is
avoided across sessions.

Cache invalidation:
- Explicit: when a new {product, version} is published (Phase 4 / docs_store
  `activate_doc(deprecate_prior=True)`), we drop all keys for the prior version.
- TTL: 24 h default — long enough that hot specs stay warm across the day,
  short enough that stale entries don't linger after a manual delete.

The session layer NEVER stores a per-user doc copy — only a pointer
(cache key) lives in the conversation state.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from core.config import RDB_CACHE
from core.kv import get_kv
from core.logger import logger


_TTL_SECONDS = int(os.getenv("KB_DOC_CACHE_TTL_SECONDS", "86400"))   # 24 h
_PREFIX      = "kb:doc:v1:"
_INDEX_KEY   = "kb:doc:v1:index"   # set of all cache keys for bulk invalidation


def _kv():
    return get_kv(RDB_CACHE, decode_responses=True)


def make_key(product_id: Optional[str], spec_version: Optional[str], doc_id: str) -> str:
    """
    Compose the Redis key for a {product, version, doc} triple.

    All three components are part of the key so the same doc can coexist under
    multiple {product, version} contexts without collision (e.g. a spec that was
    re-attached to a different product on re-upload).
    """
    pid = (product_id or "_").strip().lower()
    ver = (spec_version or "_").strip().lower()
    return f"{_PREFIX}{pid}:{ver}:{doc_id}"


def _make_section_map(text: str) -> list[dict]:
    """
    Build a flat list of {section_path, level, start, end} entries by walking
    the markdown headings. Empty list when the doc has no headings.

    The section map drives Coverage-tier graph traversal and the §8y
    section-coverage-ratio signal without re-parsing the doc on every query.

    Heading detection — two patterns are recognised:

    1. Markdown headings  : lines starting with one or more # characters
       e.g.  ## Arbitration Time Limits

    2. Bold-only lines    : lines where the entire content is wrapped in **...**
       and the text is short (≤ 120 chars) with no sentence-ending punctuation.
       These appear in documents (e.g. policy manuals) where section titles were
       written as bold Normal-style paragraphs and the legacy parse_docx emitted
       them as plain bold markdown rather than # headings.
       e.g.  **Arbitration Time Limits**  → treated as level-2 heading (##)

    Bold-line detection is only applied when the document has zero # headings,
    to avoid double-counting in documents that already use proper # headings.
    """
    import re
    out: list[dict] = []
    heading_stack: list[tuple[int, str]] = []
    head_re = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$", re.MULTILINE)

    matches = list(head_re.finditer(text))

    # ── Fallback: detect **bold-only lines** as pseudo-headings ──────────────
    # Applied only when the document has no # headings at all (0 matches).
    # This handles legacy-indexed documents where parse_docx emitted bold
    # section titles as plain text instead of # headings.
    if not matches:
        bold_re = re.compile(
            r"^(?P<stars>\*{2})(?P<text>[^*\n]{1,120}?)(?P=stars)\s*$",
            re.MULTILINE,
        )
        bold_matches = list(bold_re.finditer(text))
        for i, m in enumerate(bold_matches):
            htext = m.group("text").strip()
            # Skip if it looks like a sentence (ends with punctuation)
            if not htext or htext[-1] in ".!?,:;":
                continue
            level = 2   # treat all bold-only lines as level-2 (##)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, htext))
            path  = " > ".join(h[1] for h in heading_stack)
            start = m.start()
            end   = bold_matches[i + 1].start() if i + 1 < len(bold_matches) else len(text)
            out.append({
                "section_path": path,
                "level":        level,
                "start":        start,
                "end":          end,
            })
        return out

    # ── Primary path: standard # heading detection ────────────────────────────
    for i, m in enumerate(matches):
        level = len(m.group("hashes"))
        htext = m.group("text").strip()
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, htext))
        path = " > ".join(h[1] for h in heading_stack)
        start = m.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append({
            "section_path": path,
            "level":        level,
            "start":        start,
            "end":          end,
        })
    return out


def load(product_id: Optional[str], spec_version: Optional[str], doc_id: str) -> Optional[dict]:
    """
    Return the cached payload for a {product, version, doc} triple, or None.

    Payload schema:
      {
        "doc_id": str,
        "product_id": str|None,
        "spec_version": str|None,
        "name": str,
        "fs_path": str,           # KB_DOC_STORAGE_PATH/<doc_id>.md (canonical)
        "full_md": str,
        "md_sha": str,
        "section_map": [{section_path, level, start, end}, ...],
        "char_len": int,
      }
    """
    key = make_key(product_id, spec_version, doc_id)
    try:
        raw = _kv().get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"kb_doc_cache.load failed for {key}: {e}")
        return None


def warm(doc_id: str) -> Optional[dict]:
    """
    Load doc from Postgres + on-disk MD body, build section map, persist to Redis.

    The full doc body lives at KB_DOC_STORAGE_PATH/<doc_id>.md on the local
    filesystem (kn_rewrite.md §6). The path is implicit from doc_id — no DB
    column stores it. If the file is missing (e.g. on a fresh install whose
    KB_DOC_STORAGE_PATH was never mounted), we fall back to
    KnowledgeDocument.content (the upload-time preview).

    Returns the cached payload on success, None on failure. Safe to call
    concurrently — last write wins (payload is byte-identical for the same doc).
    """
    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        from core.config import KB_DOC_STORAGE_PATH as _KB_FS_ROOT
        import os as _os_warm

        db = SessionLocal()
        try:
            doc = db.get(KnowledgeDocument, doc_id)
            if not doc:
                logger.warning(f"kb_doc_cache.warm: doc {doc_id} not found")
                return None
            full_md       = doc.content or ""
            product_id    = str(doc.product_id) if doc.product_id else None
            spec_version  = doc.spec_version
            name          = doc.name or doc.filename
        finally:
            db.close()

        # Read the canonical body off disk. The filesystem copy is always
        # preferred over the DB content because:
        #   1. activate_doc() writes the full Docling-parsed markdown to disk.
        #   2. doc.content (DB) may be a truncated preview on older rows.
        #   3. Both come from the same source so lengths are usually equal —
        #      the old guard `len(_md) > len(full_md)` always evaluated False
        #      (equal lengths) and silently discarded the file even when it
        #      was successfully read. Fixed to `if _md:` — always prefer disk.
        _fs_path = _os_warm.path.join(_KB_FS_ROOT, f"{doc_id}.md")
        try:
            with open(_fs_path, "rb") as _fh:
                _bytes = _fh.read()
            if _bytes:
                _md = _bytes.decode("utf-8", errors="replace")
                if _md:                  # always prefer the filesystem copy
                    full_md = _md
                    logger.info(
                        f"kb_doc_cache.warm: loaded {len(_md):,} chars "
                        f"from disk for doc {doc_id}"
                    )
        except FileNotFoundError:
            logger.warning(
                f"kb_doc_cache.warm: {_fs_path} not on disk — using DB content "
                f"(legacy/pre-Phase-3 doc?). Coverage will use DB preview only."
            )
        except Exception as _fse:
            logger.warning(
                f"kb_doc_cache.warm: filesystem read failed for {_fs_path} "
                f"(using DB copy): {_fse}"
            )

        section_map = _make_section_map(full_md)
        payload = {
            "doc_id":       doc_id,
            "product_id":   product_id,
            "spec_version": spec_version,
            "name":         name,
            "fs_path":      _fs_path,
            "full_md":      full_md,
            "md_sha":       hashlib.sha256(full_md.encode("utf-8", errors="ignore")).hexdigest(),
            "section_map":  section_map,
            "char_len":     len(full_md),
        }

        key = make_key(product_id, spec_version, doc_id)
        try:
            kv = _kv()
            kv.setex(key, _TTL_SECONDS, json.dumps(payload))
            kv.sadd(_INDEX_KEY, key)
        except Exception as e:
            logger.warning(f"kb_doc_cache.warm: Redis write failed for {key}: {e}")

        logger.info(
            f"kb_doc_cache.warm: doc {doc_id} cached "
            f"({payload['char_len']:,} chars, {len(section_map)} sections, key={key})"
        )
        return payload
    except Exception as e:
        logger.error(f"kb_doc_cache.warm failed for doc {doc_id}: {e}")
        return None


def get_or_warm(product_id: Optional[str], spec_version: Optional[str], doc_id: str) -> Optional[dict]:
    """Return cached payload if present, otherwise warm + return.

    When called from product-level scope (spec_version=None), the caller's
    lookup key uses "_" as the version placeholder. But warm() stores the
    payload under the doc's own spec_version (e.g. "v1") read from the DB.
    This causes a permanent cache miss for every product-level query.

    Fix: after warm() succeeds, also write an alias key under the caller's
    (product_id, spec_version) so subsequent product-level lookups hit Redis
    instead of re-warming from disk/DB on every request.
    """
    payload = load(product_id, spec_version, doc_id)
    if payload is not None:
        return payload

    payload = warm(doc_id)

    # ── Write alias key under caller's (product_id, spec_version) ────────────
    # warm() stores under the doc's own (product_id, spec_version) from the DB.
    # If the caller passed a different spec_version (e.g. None for product-level
    # scope), load() will always miss that key. Writing an alias here ensures
    # the next call hits the cache directly without re-warming.
    if payload is not None:
        caller_key = make_key(product_id, spec_version, doc_id)
        doc_key    = make_key(payload.get("product_id"), payload.get("spec_version"), doc_id)
        if caller_key != doc_key:
            try:
                kv = _kv()
                kv.setex(caller_key, _TTL_SECONDS, json.dumps(payload))
                kv.sadd(_INDEX_KEY, caller_key)
                logger.info(
                    f"kb_doc_cache.get_or_warm: wrote alias key {caller_key} "
                    f"(doc key={doc_key})"
                )
            except Exception as _alias_err:
                logger.warning(
                    f"kb_doc_cache.get_or_warm: alias key write failed "
                    f"({_alias_err}) — next call will re-warm (non-fatal)"
                )

    return payload


def invalidate(doc_id: str) -> int:
    """
    Drop every cached entry for a doc_id (across any {product, version} context).

    Returns the number of keys removed. Called from docs_store.delete_doc and
    from the deprecate_prior flow when a new version supersedes an older one.
    """
    try:
        kv = _kv()
        members = list(kv.smembers(_INDEX_KEY) or [])
        suffix = f":{doc_id}"
        dropped = 0
        for k in members:
            if k.endswith(suffix):
                try:
                    kv.delete(k)
                    kv.srem(_INDEX_KEY, k)
                    dropped += 1
                except Exception:
                    pass
        if dropped:
            logger.info(f"kb_doc_cache.invalidate: dropped {dropped} key(s) for doc {doc_id}")
        return dropped
    except Exception as e:
        logger.warning(f"kb_doc_cache.invalidate failed for {doc_id}: {e}")
        return 0


def invalidate_product_version(product_id: str, spec_version: str) -> int:
    """
    Drop every cached entry for a {product, version} pair. Used by the
    deprecate_prior flow when a new version takes over.
    """
    try:
        kv = _kv()
        members = list(kv.smembers(_INDEX_KEY) or [])
        prefix = f"{_PREFIX}{(product_id or '_').lower()}:{(spec_version or '_').lower()}:"
        dropped = 0
        for k in members:
            if k.startswith(prefix):
                try:
                    kv.delete(k)
                    kv.srem(_INDEX_KEY, k)
                    dropped += 1
                except Exception:
                    pass
        if dropped:
            logger.info(
                f"kb_doc_cache.invalidate_product_version: dropped {dropped} key(s) "
                f"for product={product_id} version={spec_version}"
            )
        return dropped
    except Exception as e:
        logger.warning(f"kb_doc_cache.invalidate_product_version failed: {e}")
        return 0
