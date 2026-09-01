# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CONFLUENCE INDEXER
# Fetches all pages from a Confluence space and indexes them
# into pgvector (document_embeddings, repo = docs_kb:confluence_{space})
# ============================================================

import os
import re
import json
import base64
import uuid
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

from core.logger import logger

# ============================================================
# CONFIGURATION
# ============================================================

COLLECTION_NAME = "confluence_pages"   # kept for return-value compat
PAGE_LIMIT = 50         # results per Confluence API page
MAX_TEXT_CHARS = 2000   # truncation limit per document


# ============================================================
# HELPERS
# ============================================================

def _strip_html(raw: str) -> str:
    """Remove HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _fetch_json(path: str, email: str, token: str) -> dict:
    """
    Perform a Confluence GET and return parsed JSON.

    Production (LLM_PROXY_URL set): routes through the LLM proxy server LLM proxy because
    Confluence Cloud is not reachable from the gateway server directly.

    Local dev (LLM_PROXY_URL unset): calls Confluence directly using CONFLUENCE_URL.
    The ``path`` argument is the API path, e.g. /rest/api/content?spaceKey=...
    """
    proxy_url = os.environ.get("LLM_PROXY_URL", "").rstrip("/")
    if proxy_url:
        import httpx
        # ── Correlation ID propagation ─────────────────────────────────────────
        # Carry request_id / chat_id from the indexer job's thread-local context
        # into the llm_proxy so every Atlassian API call is traceable back to the
        # originating index job.
        _proxy_body: dict = {
            "service": "confluence", "method": "GET", "path": path,
            "email": email, "token": token,
        }
        try:
            from core.logger import get_request_id, get_chat_id
            _rid = get_request_id()
            _cid = get_chat_id()
            if _rid and _rid != "-":
                _proxy_body["request_id"] = _rid
            if _cid and _cid != "-":
                _proxy_body["chat_id"] = _cid
        except Exception:
            pass
        # ──────────────────────────────────────────────────────────────────────
        from core.proxy_tool_use import llm_proxy_headers as _lph
        resp = httpx.post(
            f"{proxy_url}/atlassian/proxy",
            json=_proxy_body,
            headers=_lph(),
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()
    else:
        base_url = os.environ.get("CONFLUENCE_URL", "").rstrip("/")
        full_url = f"{base_url}{path}"
        auth_header = f"Basic {base64.b64encode(f'{email}:{token}'.encode()).decode()}"
        req = urllib.request.Request(full_url, headers={
            "Authorization": auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        resp = urllib.request.urlopen(req, timeout=30)
        try:
            return json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()


# ============================================================
# MAIN INDEXER
# ============================================================

def index_confluence_space(space_key: str = None) -> dict:
    """
    Fetch all pages from a Confluence space and index them into
    pgvector (document_embeddings, repo = docs_kb:confluence_{space_key}).

    Parameters
    ----------
    space_key : str, optional
        The Confluence space key to index (e.g. "ENG").
        Falls back to the CONFLUENCE_SPACE_KEY environment variable.

    Returns
    -------
    dict
        {indexed, skipped, errors, collection}
    """

    # --------------------------------------------------------
    # Resolve credentials
    # --------------------------------------------------------
    base_url   = os.environ.get("CONFLUENCE_URL", "").rstrip("/")
    email      = os.environ.get("CONFLUENCE_EMAIL", "")
    token      = os.environ.get("CONFLUENCE_API_TOKEN", "")
    space      = space_key or os.environ.get("CONFLUENCE_SPACE_KEY", "")

    if not base_url or not email or not token:
        msg = "Confluence credentials missing (CONFLUENCE_URL / CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN)"
        logger.error(f"ConfluenceIndexer: {msg}")
        return {"indexed": 0, "skipped": 0, "errors": 0, "collection": COLLECTION_NAME, "error": msg}

    if not space:
        msg = "No space_key provided and CONFLUENCE_SPACE_KEY is not set"
        logger.error(f"ConfluenceIndexer: {msg}")
        return {"indexed": 0, "skipped": 0, "errors": 0, "collection": COLLECTION_NAME, "error": msg}

    # --------------------------------------------------------
    # pgvector setup (embed svc + document_embeddings)
    # --------------------------------------------------------
    # No hardcoded localhost default — same env var as core.config.EMBED_SVC_URL.
    _EMBED_SVC = os.getenv("EMBED_SVC_URL", "")
    _repo_key  = f"docs_kb:confluence_{space.lower()}"

    def _pgvector_upsert(texts, ids, metas):
        """Embed texts via embed svc and upsert into document_embeddings."""
        try:
            import httpx as _httpx
            resp = _httpx.post(f"{_EMBED_SVC}/embed", json={"texts": texts, "provider": "ollama"}, timeout=120.0)
            resp.raise_for_status()
            embeddings = resp.json()["embeddings"]
        except Exception as e:
            logger.error(f"ConfluenceIndexer: embed svc failed: {e}")
            return 0

        from db.database import VectorSessionLocal
        from db.models import DocumentEmbedding
        vdb = VectorSessionLocal()
        try:
            for text, emb, page_id, meta in zip(texts, embeddings, ids, metas):
                row = DocumentEmbedding(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"confluence:{page_id}")),
                    repo=_repo_key,
                    file_path=meta.get("url", page_id),
                    chunk_index=0,
                    content=text,
                    embedding=emb,
                    metadata_=meta,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    classification="INTERNAL",
                    uploaded_by="confluence-indexer",
                    created_at=datetime.utcnow(),
                )
                vdb.merge(row)
            vdb.commit()
            try:
                from core.config import RDB_CACHE
                from core.kv import get_kv
                _r = get_kv(RDB_CACHE, decode_responses=True)
                _r.sadd("docs:namespaces", f"confluence_{space.lower()}")
            except Exception:
                pass
            return len(texts)
        except Exception as e:
            vdb.rollback()
            logger.error(f"ConfluenceIndexer: pgvector insert failed: {e}")
            return 0
        finally:
            vdb.close()

    # --------------------------------------------------------
    # Pagination loop
    # --------------------------------------------------------
    indexed = 0
    skipped = 0
    errors  = 0
    start   = 0

    logger.info(f"ConfluenceIndexer: starting index for space '{space}'")

    while True:
        params = urllib.parse.urlencode({
            "spaceKey": space,
            "limit":    PAGE_LIMIT,
            "start":    start,
            "expand":   "body.storage,version",
            "type":     "page",
        })
        path = f"/rest/api/content?{params}"

        try:
            data = _fetch_json(path, email, token)
        except urllib.error.HTTPError as e:
            logger.error(f"ConfluenceIndexer: HTTP {e.code} fetching pages at start={start}: {e}")
            errors += 1
            break
        except Exception as e:
            logger.error(f"ConfluenceIndexer: fetch failed at start={start}: {e}")
            errors += 1
            break

        results = data.get("results", [])
        if not results:
            break

        # ------------------------------------------------
        # Process each page
        # ------------------------------------------------
        doc_ids    = []
        doc_texts  = []
        doc_metas  = []

        for page in results:
            try:
                page_id    = str(page.get("id", ""))
                title      = page.get("title", "")
                page_url   = f"{base_url}/wiki/spaces/{space}/pages/{page_id}"

                # Extract body text
                body_storage = (
                    page.get("body", {})
                        .get("storage", {})
                        .get("value", "")
                )
                raw_text = _strip_html(body_storage)

                if not raw_text:
                    skipped += 1
                    continue

                # Truncate
                text = (raw_text[:MAX_TEXT_CHARS] + "...") if len(raw_text) > MAX_TEXT_CHARS else raw_text

                # Structured prefix improves embedding quality
                doc_text = (
                    f"Space: {space}\n"
                    f"Title: {title}\n"
                    f"URL: {page_url}\n\n"
                    f"{text}"
                )

                doc_ids.append(page_id)
                doc_texts.append(doc_text)
                doc_metas.append({
                    "title":      title,
                    "url":        page_url,
                    "space":      space,
                    "page_id":    page_id,
                    "source":     "confluence",
                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                })

            except Exception as e:
                logger.error(f"ConfluenceIndexer: error processing page {page.get('id')}: {e}")
                errors += 1

        # ------------------------------------------------
        # Batch upsert into pgvector
        # ------------------------------------------------
        if doc_ids:
            n = _pgvector_upsert(doc_texts, doc_ids, doc_metas)
            if n:
                indexed += n
                logger.info(f"ConfluenceIndexer: indexed {n} pages (total so far: {indexed})")
            else:
                errors += len(doc_ids)

        # ------------------------------------------------
        # Pagination bookkeeping
        # ------------------------------------------------
        size  = data.get("size", 0)
        total = data.get("totalSize", data.get("total", 0))
        start += size

        if start >= total or size == 0:
            break

    logger.info(
        f"ConfluenceIndexer: finished — indexed={indexed} skipped={skipped} errors={errors}"
    )
    return {
        "indexed":    indexed,
        "skipped":    skipped,
        "errors":     errors,
        "collection": COLLECTION_NAME,
    }
