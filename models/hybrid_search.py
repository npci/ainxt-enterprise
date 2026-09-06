# SPDX-License-Identifier: MIT
# ============================================================
# ENTERPRISE HYBRID SEARCH ENGINE
# ============================================================

from core.logger import logger
from core.config import EMBED_SVC_URL as _CORE_EMBED_SVC_URL
import time
import json as _json
import os

# ============================================================
# EMBED SERVICE CLIENT
# ============================================================
#
# All embedding calls route through the embed svc microservice on :8001.
# The svc handles batching, caching, and provider routing internally.
# Fallback: direct Ollama HTTP if embed svc is not running.
#
# This replaces the old lazy _get_embed_model() singleton which caused:
#   - HuggingFace download during a live request → segfault
#   - MPS device init in a FastAPI thread → crash
#   - Blocking event loop in uvicorn workers

import httpx as _httpx

# Note on the ``metadata`` + ``chunk_index`` columns surfaced by both
# ``keyword_search`` and ``pgvector_search``: they exist purely to support
# ABStudio Build Studio's per-workflow doc-id scoping in
# ``AgentStudio/backend/app/core/kb_retriever.py``. The fields are read by key
# from the output dict, so existing callers (gateway chat RAG,
# sdlc_coder_tools, etc.) transparently ignore them. Single source of
# truth for the rationale — kept here so future edits to the SELECT
# statements don't drop the columns by accident.

_ATOMIC_CONTENT_TYPES = {"json", "code", "xml", "code_like"}


def _search_result_content(content: str, metadata: dict) -> str:
    if not content:
        return ""
    meta = metadata or {}
    if meta.get("atomic") is True or str(meta.get("content_type") or "").lower() in _ATOMIC_CONTENT_TYPES:
        return content
    return content[:1500]


_EMBED_SVC_URL  = _CORE_EMBED_SVC_URL
_OLLAMA_URL     = os.getenv("OLLAMA_URL",    "")
_OLLAMA_MODEL   = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text:latest")

# Persistent sync client — reuses TCP connection across calls
_embed_http = _httpx.Client(
    base_url=_EMBED_SVC_URL,
    timeout=30.0,
    limits=_httpx.Limits(max_connections=10, max_keepalive_connections=10),
)


def _embed_svc(texts: list[str], provider: str = "ollama") -> list[list[float]]:
    """
    Call embed svc for a batch of texts. Raises on failure — callers must handle.
    No direct Ollama fallback: embed svc is the only embedding path in prod.
    """
    resp = _embed_http.post(
        "/embed",
        json={"texts": texts, "provider": provider},
    )
    resp.raise_for_status()
    return resp.json()["embeddings"]


# ============================================================
# SEMANTIC SEARCH
# ============================================================

def semantic_search(repo_filter, question, retriever_getter):
    """
    Enterprise semantic retrieval using embedding retriever.
    Includes timing, safety, and normalized scoring.
    """

    start = time.time()

    try:

        retriever = retriever_getter(repo_filter)

        if not retriever:
            logger.warning("Semantic search: no retriever")
            return []

        nodes = retriever.retrieve(
            QueryBundle(query_str=question)
        )

        results = []

        for node in nodes:

            try:
                text = node.node.text.strip()

                if not text:
                    continue

                score = float(node.score or 0.0)

                results.append({
                    "text": text,
                    "score": score,
                    "source": "semantic",
                    "repo": repo_filter
                })

            except Exception:
                continue

        latency = time.time() - start

        logger.info(
            f"[kb_retrieval] semantic search repo={repo_filter} results={len(results)} latency={latency:.3f}s"
        )

        return results

    except Exception as e:

        logger.error(f"Semantic search failed: {e}")

        return []


# ============================================================
# KEYWORD SEARCH — Postgres BM25 via tsvector/tsquery
# ============================================================
# Maximum characters of the question sent to websearch_to_tsquery.
# Postgres internal tsquery parser stack overflows on very long inputs
# (error: "tsquery stack too small"). 500 chars is well within safe limits
# and still captures all meaningful search terms in any real question.
_BM25_QUERY_MAX_CHARS = 500
def _normalize_repo_key(raw: str) -> str:
    """Normalize a single repo string to the document_embeddings key format."""
    r = (raw or "global").lower()
    if r and r not in ("global",) and not r.startswith("docs_kb:") and not r.startswith("agent_kb:"):
        _rp = r.split("/")[-1].replace("-", "_").replace(".", "_")
        r = _rp if _rp.startswith("repo_") else f"repo_{_rp}"
    return r


def keyword_search(repo_filter, question, user_ctx: dict = None, file_filter: list = None):
    """
    Real BM25 keyword search using Postgres full-text search (tsvector/tsquery).
    ts_rank is BM25-equivalent. GIN index on content makes this fast.
    Replaces ChromaDB approximate keyword search.

    repo_filter — scalar string OR list of strings. When a list, SQL uses
    WHERE repo = ANY(:repos) so all repos are searched in one query.

    user_ctx — optional dict with keys: user_id, user_role, org_id, session_id,
               band_level, product_ids.
    When provided, visibility/band/product filters are applied in SQL WHERE clause.
    classification/org_id/allowed_roles checks remain as a Python post-filter.
    """
    start = time.time()
    try:
        from db.database import VectorReadSessionLocal as VectorSessionLocal
        from sqlalchemy import text as _sql

        # Guard: Postgres websearch_to_tsquery raises "tsquery stack too small"
        # when given a very long input (e.g. the full system prompt was passed
        # instead of just the user question). Truncate to a safe length so only
        # the meaningful search terms reach the tsquery parser.
        if question and len(question) > _BM25_QUERY_MAX_CHARS:
            logger.warning(
                f"BM25 keyword_search: question truncated from {len(question)} "
                f"to {_BM25_QUERY_MAX_CHARS} chars to prevent tsquery stack overflow "
                f"(repo={repo_filter}). Ensure only the user query — not the full "
                f"prompt — is passed to keyword_search()."
            )
            question = question[:_BM25_QUERY_MAX_CHARS]

        # Determine whether we have a multi-repo list or a scalar filter.
        _is_list = isinstance(repo_filter, list)

        if _is_list:
            # Multi-repo path: normalize each entry and build ANY(:repos) clause.
            _repo_keys = [_normalize_repo_key(r) for r in repo_filter] if repo_filter else ["global"]
            repo_scope_sql    = "LOWER(repo) = ANY(:repos)"
            repo_scope_params = {"repos": _repo_keys}
            # Use the first entry for logging; "global" if list was empty.
            repo = _repo_keys[0] if _repo_keys else "global"
        else:
            # Scalar path: existing normalization logic (unchanged).
            repo = _normalize_repo_key(repo_filter)

            # Determine search scope: product-wide (cross-repo) vs single-repo
            # When no repo_filter is given AND the user has product mappings, search
            # across ALL repos belonging to the user's products instead of "global".
            _product_ids = (user_ctx.get("product_ids") or []) if user_ctx else []
            if repo == "global" and _product_ids:
                repo_scope_sql    = "product_id::text = ANY(:pids)"
                repo_scope_params = {"pids": _product_ids}
            else:
                repo_scope_sql    = "LOWER(repo) = LOWER(:repo)"
                repo_scope_params = {"repo": repo}

        # product_ids scope only applies on the scalar path (global, no repo_filter).
        # On the list path the caller already scoped the repos explicitly.

        # Build SQL ACL predicates when user_ctx is provided
        acl_sql = ""
        acl_params = {}
        if user_ctx:
            is_admin  = user_ctx.get("is_admin", False) or (user_ctx.get("user_role", "") == "admin")
            user_dept = user_ctx.get("department", "") or ""

            # department IS NULL/'' → org-wide, visible to all
            # :is_admin             → admin bypasses dept filter
            # department = dept     → exact dept match
            # metadata.department_ids contains dept → multi-dept PRIVATE KB
            # NOTE: no "OR :user_dept = ''" — user with no dept sees only org-wide docs
            acl_sql = """
                      AND (
                          department IS NULL
                          OR department = ''
                          OR :is_admin = true
                          OR department = :user_dept
                          OR (
                              :user_dept <> ''
                              AND jsonb_exists(metadata, 'department_ids')
                              AND jsonb_exists(metadata->'department_ids', :user_dept)
                          )
                      )"""
            acl_params = {
                "is_admin":  is_admin,
                "user_dept": user_dept,
            }

        # Phase 1 — Hard scope filter (product + domain + spec_version + doc_id)
        # Injected via user_ctx["scope_filter"] by hybrid_retriever when a KB query
        # carries explicit product/domain/version scope. Deterministic server-side
        # filter — never client-spoofable. Kills cross-product hallucination.
        # When kb_doc_id is set (user drilled to a specific document), an additional
        # metadata->>'doc_id' filter is applied so retrieval is strictly limited to
        # chunks from that document only — other docs in the same namespace are excluded.
        scope_sql    = ""
        scope_params = {}
        _scope = (user_ctx or {}).get("scope_filter", {})
        _scope_clauses = []
        if _scope:
            if _scope.get("product_id"):
                _scope_clauses.append("product_id::text = :scope_product_id")
                scope_params["scope_product_id"] = str(_scope["product_id"])
            if _scope.get("domain"):
                _scope_clauses.append("domain = :scope_domain")
                scope_params["scope_domain"] = _scope["domain"]
            if _scope.get("spec_version"):
                _scope_clauses.append("spec_version = :scope_spec_version")
                scope_params["scope_spec_version"] = _scope["spec_version"]
        # Document-level isolation: only active when the user drilled down to a
        # specific KB document. Has no effect at domain/product/version level
        # (kb_doc_id is null/absent for those scopes).
        _kb_doc_id = (user_ctx or {}).get("kb_doc_id")
        if _kb_doc_id:
            _scope_clauses.append("metadata->>'doc_id' = :scope_doc_id")
            scope_params["scope_doc_id"] = str(_kb_doc_id)
        if _scope_clauses:
            scope_sql = "AND " + " AND ".join(_scope_clauses)

        # Optional file-scope filter from graph resolver
        graph_sql    = ""
        graph_params = {}
        if file_filter:
            graph_sql    = "AND file_path = ANY(:file_filter)"
            graph_params = {"file_filter": file_filter}

        db = VectorSessionLocal()
        try:
            # Build an OR-based tsquery so BM25 returns results when not all query terms
            # co-occur in the same chunk.  websearch_to_tsquery uses AND between unquoted
            # words; adding explicit OR between significant terms gives recall for
            # natural-language questions over code (e.g. "how does quiz component work").
            import re as _re
            _words = _re.findall(r'\b[a-zA-Z]\w{2,}\b', question)
            _or_query = " OR ".join(_words) if _words else question

            tsq_row = db.execute(
                _sql("SELECT websearch_to_tsquery('english', :q)::text"),
                {"q": _or_query},
            ).fetchone()
            tsq_str = tsq_row[0] if tsq_row else ""
            if not tsq_str:
                logger.warning(
                    f"BM25 skipped (repo={repo_filter}): all words in question are "
                    f"English stop words — tsquery is empty. Question: {question[:80]!r}"
                )
                return []
            logger.debug(f"BM25 tsquery: {tsq_str!r}")

            # ── Part U12 (docx §4) — exact-term BM25 path ──────────────────
            # The english regex above destroys identifiers like 'RBI/2024-25/12'
            # (only 'RBI' survives) and drops quoted phrases. Detect both
            # patterns and route them through the no-stemming 'simple'
            # tsvector (content_simple_tsv, GENERATED at U12) so the literal
            # tokens match. The english path still runs unconditionally so
            # conceptual queries (where stemming helps) are unaffected.
            #
            # Quoted phrase  : "settlement exception"      → phraseto_tsquery
            # Identifier-shape: contains letters+digits or punctuation
            #                  (RBI/2024-25/12, FASTag2.0, circular-2024-25)
            _phrases = _re.findall(r'"([^"]+)"', question or "")
            _identifiers = [
                _id for _id in _re.findall(r'[A-Za-z][\w./-]*\d[\w./-]*', question or "")
                if any(ch in _id for ch in "/.-") or _re.search(r'\d', _id)
            ]
            _exact_terms: list[str] = []
            for _p in _phrases:
                _p = _p.strip()
                if _p:
                    _exact_terms.append(_p)
            for _id in _identifiers:
                # Skip identifiers that are pure ASCII words already covered
                # by the english path (regex enforces a digit OR punctuation,
                # but be defensive — bare 'FASTag' has no digit so won't reach
                # here from the regex; only digit/punctuation forms arrive).
                if _id and _id not in _exact_terms:
                    _exact_terms.append(_id)
            _exact_query = " ".join(_exact_terms) if _exact_terms else None
            if _exact_query:
                logger.debug(
                    f"BM25 exact-term path engaged — phrases={_phrases} "
                    f"identifiers={_identifiers}"
                )

            # Phase 1 closure — chunk-level active-version filter. Deprecated
            # chunks (flipped by docs_store.activate_doc deprecate_prior branch)
            # never participate in retrieval. The partial index
            # idx_doc_embed_status keeps this cheap. Hand-coded constant — no
            # bind param, no scope-condition complexity.
            status_sql = "AND status = 'ACTIVE'"

            # The english path is always present. When the caller's question
            # contains an identifier/phrase, we UNION ALL the simple-tsv hits
            # with a 1.5× rank boost so exact-match identifiers outrank
            # stem-match hits at the same content (the rerank later can still
            # reorder; this just makes sure both candidate sets reach it).
            # Hierarchy columns added per Part U11 — pulled through to the
            # output dict so the gateway citation payload doesn't need a
            # second SELECT. Empty strings on legacy (pre-U11) chunks.
            _hier_cols = (
                "doc_name, section_name, section_path, page_number, source_type, "
                "metadata->>'doc_id' AS doc_id"
            )
            if _exact_query:
                _sql_text = f"""
                    SELECT id, content, MAX(rank) AS rank,
                           classification, org_id, allowed_roles, allowed_users,
                           file_path, metadata, chunk_index,
                           doc_name, section_name, section_path, page_number,
                           source_type, doc_id
                    FROM (
                        -- english (stemmed) — original path, unchanged.
                        SELECT id, content,
                               ts_rank(to_tsvector('english', content),
                                       websearch_to_tsquery('english', :q)) AS rank,
                               classification, org_id, allowed_roles, allowed_users,
                               file_path, metadata, chunk_index,
                               {_hier_cols}
                        FROM document_embeddings
                        WHERE {repo_scope_sql}
                          AND to_tsvector('english', content)
                              @@ websearch_to_tsquery('english', :q)
                        {acl_sql}
                        {scope_sql}
                        {status_sql}
                        {graph_sql}

                        UNION ALL

                        -- simple (no stemming, no stop-words) — exact-term path.
                        -- phraseto_tsquery enforces token order so "settlement
                        -- exception" matches as a phrase, not OR-ed words.
                        -- The 1.5× boost lets exact-match identifiers float
                        -- above conceptual stem matches in the rerank pool.
                        SELECT id, content,
                               ts_rank(content_simple_tsv,
                                       phraseto_tsquery('simple', :qx)) * 1.5 AS rank,
                               classification, org_id, allowed_roles, allowed_users,
                               file_path, metadata, chunk_index,
                               {_hier_cols}
                        FROM document_embeddings
                        WHERE {repo_scope_sql}
                          AND content_simple_tsv @@ phraseto_tsquery('simple', :qx)
                        {acl_sql}
                        {scope_sql}
                        {status_sql}
                        {graph_sql}
                    ) merged
                    GROUP BY id, content, classification, org_id,
                             allowed_roles, allowed_users, file_path, metadata, chunk_index,
                             doc_name, section_name, section_path, page_number,
                             source_type, doc_id
                    ORDER BY rank DESC
                    LIMIT 60
                """
                rows = db.execute(
                    _sql(_sql_text),
                    {
                        "q": _or_query, "qx": _exact_query,
                        **repo_scope_params, **acl_params,
                        **scope_params, **graph_params,
                    },
                ).fetchall()
            else:
                # No quoted phrases / identifiers — original single-path query.
                # IMPORTANT: SELECT list MUST match the exact-term branch above
                # (and the row[…] unpacking at L420+) — that means metadata and
                # chunk_index BEFORE the _hier_cols block. Previously these two
                # columns were missing in this branch, which made every plain
                # English question (no phrases, no identifiers-with-digits)
                # crash with "tuple index out of range" at row[15] and BM25
                # silently returned []. Fixed by aligning the column order.
                rows = db.execute(
                    _sql(f"""
                        SELECT id, content,
                               ts_rank(to_tsvector('english', content),
                                       websearch_to_tsquery('english', :q)) AS rank,
                               classification, org_id, allowed_roles, allowed_users,
                               file_path, metadata, chunk_index,
                               {_hier_cols}
                        FROM document_embeddings
                        WHERE {repo_scope_sql}
                          AND to_tsvector('english', content)
                              @@ websearch_to_tsquery('english', :q)
                        {acl_sql}
                        {scope_sql}
                        {status_sql}
                        {graph_sql}
                        ORDER BY rank DESC
                        LIMIT 60
                    """),
                    {"q": _or_query, **repo_scope_params, **acl_params, **scope_params, **graph_params},
                ).fetchall()
        finally:
            db.close()

        # ts_rank is not normalised; divide by (rank+1) to get [0,1) approximation
        output = []
        for row in rows:
            chunk_id, content, rank = row[0], row[1], float(row[2])
            if not content:
                continue

            if user_ctx:
                # SQL already handled visibility/band/product — only check
                # classification/org_id/allowed_roles here.
                chunk_meta = {
                    "chunk_id":     str(chunk_id),
                    "classification": row[3] or "INTERNAL",
                    "org_id":       row[4] or "",
                    "allowed_roles": row[5] or [],
                    "allowed_users": row[6] or [],
                    "repo":         repo_filter,
                    "file_path":    row[7] or "",
                }
                from core.rag_acl import check_rag_access
                granted, _ = check_rag_access(
                    user_id=user_ctx.get("user_id", ""),
                    user_role=user_ctx.get("user_role", "viewer"),
                    org_id=user_ctx.get("org_id", ""),
                    chunk_metadata=chunk_meta,
                    session_id=user_ctx.get("session_id", ""),
                    query_text=question,
                )
                if not granted:
                    continue

            row_metadata = row[8] or {}
            output.append({
                "chunk_id":     str(chunk_id),
                "text":         _search_result_content(content, row_metadata),
                "score":        rank / (rank + 1),
                "source":       "bm25",
                "repo":         repo_filter,
                "file_path":    row[7] or "",
                "metadata":     row_metadata,
                "chunk_index":  row[9],
                # Part U11 — hierarchy metadata for citation render
                "doc_name":     row[10],
                "section_name": row[11],
                "section_path": row[12],
                "page_number":  row[13],
                "source_type":  row[14],
                "doc_id":       row[15],
            })

        latency = time.time() - start
        if not output:
            logger.warning(
                f"BM25 returned 0 results (repo={repo_filter} latency={latency:.3f}s) — "
                f"tsquery={tsq_str!r} raw_rows={len(rows)}"
            )
        else:
            logger.info(
                f"[kb_retrieval] BM25 search repo={repo_filter} results={len(output)} latency={latency:.3f}s"
            )
        return output

    except Exception as e:
        err_str = str(e)
        if "statement timeout" in err_str.lower() or "canceling statement" in err_str.lower():
            logger.error(
                f"BM25 TIMEOUT (repo={repo_filter}) — GIN index build likely failed "
                f"due to statement_timeout. Run python db/migrate.py to retry, or run "
                f"db/sql/prod_catchup_2026_04_21_bm25_gin.sql directly via psql on PGS02."
            )
        elif "invalid byte sequence" in err_str.lower() or "0x00" in err_str:
            logger.error(
                f"BM25 ENCODING ERROR (repo={repo_filter}) — content column has null "
                f"bytes or invalid UTF-8. Re-index the repo to fix stored content."
            )
        elif "tsquery stack too small" in err_str.lower():
            # Should never reach here after the truncation guard above, but kept
            # as a safety net with a clear diagnostic message.
            logger.error(
                f"BM25 TSQUERY STACK OVERFLOW (repo={repo_filter}) — query was too long "
                f"for Postgres tsquery parser even after truncation. "
                f"Query length: {len(question) if question else 0}. "
                f"Reduce _BM25_QUERY_MAX_CHARS or inspect what is being passed as 'question'."
            )
        else:
            logger.warning(f"BM25 keyword_search failed (repo={repo_filter}): {e}")
        return []


# ============================================================
# MERGE AND PRE-RANK
# ============================================================

# Reciprocal Rank Fusion over per-strategy rank lists. Score-MAX dedup (below)
# conflates incomparable vector-cosine and BM25 scales; RRF fuses RANKS instead.
# Off by default → today's score-max path is unchanged. Adoption is eval-gated.
_HYBRID_RRF_ENABLED = os.getenv("HYBRID_RRF_ENABLED", "false").lower() == "true"


def _merge_rrf(results, top_k):
    """RRF-fused merge. Groups items by `source` strategy, ranks within each by
    its native score, fuses ranks (scale-free), then returns unique items in
    fused order. Returns None on any problem so the caller falls back to
    score-max (fail-safe)."""
    try:
        from grounding.evidence import rrf_fuse
    except Exception:
        return None
    try:
        by_id: dict = {}          # id -> best item (metadata from highest native score)
        per_strategy: dict = {}   # source -> [(score, id)]
        for item in results:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            _id = str(item.get("chunk_id") or item.get("id") or text)
            score = float(item.get("score", 0.0))
            src = item.get("source", "unknown")
            if _id not in by_id or score > by_id[_id].get("score", 0.0):
                by_id[_id] = {**item, "text": text, "score": score}
            per_strategy.setdefault(src, []).append((score, _id))
        if not by_id:
            return []
        ranked_lists = [
            [i for _s, i in sorted(items, key=lambda t: t[0], reverse=True)]
            for items in per_strategy.values()
        ]
        fused_ids = rrf_fuse(ranked_lists, top_n=top_k)
        return [by_id[i] for i in fused_ids if i in by_id]
    except Exception:
        return None


def merge_and_rerank(results, top_k=20):
    """
    Merge results from semantic, keyword, metadata.
    Deduplicate and normalize scores.
    """

    start = time.time()

    # RRF path (flag-gated). Falls back to score-max below if it returns None.
    if _HYBRID_RRF_ENABLED:
        _rrf = _merge_rrf(results, top_k)
        if _rrf is not None:
            logger.info(
                f"Merge stage (RRF) input={len(results)} unique={len(_rrf)} "
                f"latency={time.time() - start:.3f}s"
            )
            return _rrf

    try:

        unique: dict = {}

        for item in results:

            text = (item.get("text") or "").strip()

            if not text:
                continue

            score = float(item.get("score", 0.0))

            if text not in unique:
                # Preserve ALL fields (file_path, chunk_id, symbol_name, etc.)
                # so reranker noise filter and LLM source citations work correctly.
                unique[text] = {**item, "text": text, "score": score}

            else:
                # Keep metadata from the higher-scoring source
                if score > unique[text]["score"]:
                    unique[text] = {**item, "text": text, "score": score}

        merged = list(unique.values())

        merged.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        latency = time.time() - start

        logger.info(
            f"Merge stage input={len(results)} unique={len(merged)} latency={latency:.3f}s"
        )

        return merged[:top_k]

    except Exception as e:

        logger.error(f"Merge failed: {e}")

        return []


# ============================================================
# PGVECTOR SIMILARITY SEARCH (high-scale ANN via Postgres HNSW)
# ============================================================

def pgvector_search(repo_filter, question, top_k=10, user_ctx: dict = None, file_filter: list = None):
    """
    Cosine similarity search using pgvector document_embeddings table.
    Falls back gracefully if pgvector extension or table is unavailable.

    repo_filter — scalar string OR list of strings. When a list, SQL uses
    WHERE repo = ANY(:repos) so all repos are searched in one query.

    user_ctx — optional dict with keys: user_id, user_role, org_id, session_id,
               band_level, product_ids.
    When provided, visibility/band/product filters are injected into SQL WHERE
    so only permitted rows are fetched from the DB.  classification/org_id/
    allowed_roles checks remain as a Python post-filter via check_rag_access().
    """

    start = time.time()

    try:
        from db.models import _PGVECTOR_AVAILABLE
        if not _PGVECTOR_AVAILABLE:
            logger.warning(
                f"[kb_retrieval] pgvector SKIPPED — pgvector extension unavailable "
                f"repo={repo_filter}"
            )
            return []

        # core.config.EMBED_PROVIDER — set once via the EMBED_PROVIDER env var,
        # read here (query-time) AND by workers/index_worker.py (index-time).
        # Deliberately the same constant in both places so the two can never
        # drift apart. MUST match the model used at index time — changing it
        # requires re-indexing (a different provider/model isn't
        # vector-comparable with what's already in document_embeddings, even
        # at the same 768 dimensions).
        from core.config import EMBED_PROVIDER
        provider = EMBED_PROVIDER
        logger.info(
            f"[kb_retrieval] pgvector search started | repo={repo_filter} provider={provider}"
        )
        try:
            emb_list = _embed_svc([question], provider=provider)
        except Exception as _emb_err:
            from core.retrieval_status import set_retrieval_warning, describe_embed_svc_error
            # Previously silent beyond this log line — a chat user got either
            # a generic "no context found" note or nothing at all, with zero
            # indication that the real cause was an unreachable/misconfigured
            # embed service rather than "nothing relevant exists". Recorded
            # here so agents/orchestrator.py can tell the user the actual
            # reason instead of guessing.
            set_retrieval_warning(
                f"Retrieval is unavailable right now ({describe_embed_svc_error(_emb_err)}) "
                f"— set EMBED_SVC_URL and confirm the embed-svc container is "
                f"running and its configured provider (EMBED_PROVIDER={provider!r}) "
                f"is actually set up. This answer has no codebase/document "
                f"context behind it."
            )
            logger.warning(
                f"[kb_retrieval] pgvector SKIPPED — embed svc unavailable | "
                f"repo={repo_filter} error={_emb_err} elapsed={time.time() - start:.3f}s"
            )
            return []
        q_vec = emb_list[0]

        from db.database import VectorReadSessionLocal as VectorSessionLocal
        from sqlalchemy import text as _sql

        # Determine whether we have a multi-repo list or a scalar filter.
        _is_list = isinstance(repo_filter, list)

        if _is_list:
            # Multi-repo path: normalize each entry and build ANY(:repos) clause.
            _repo_keys = [_normalize_repo_key(r) for r in repo_filter] if repo_filter else ["global"]
            repo_scope_sql    = "LOWER(repo) = ANY(:repos)"
            repo_scope_params = {"repos": _repo_keys}
            _repo = _repo_keys[0] if _repo_keys else "global"
        else:
            # Scalar path: existing normalization logic (unchanged).
            _repo = _normalize_repo_key(repo_filter)

            # Determine search scope: product-wide (cross-repo) vs single-repo
            _product_ids = (user_ctx.get("product_ids") or []) if user_ctx else []
            if _repo == "global" and _product_ids:
                repo_scope_sql    = "product_id::text = ANY(:pids)"
                repo_scope_params = {"pids": _product_ids}
            else:
                repo_scope_sql    = "LOWER(repo) = LOWER(:repo)"
                repo_scope_params = {"repo": _repo}

        # Build SQL ACL predicates when user_ctx is provided
        acl_sql = ""
        acl_params = {}
        if user_ctx:
            is_admin  = user_ctx.get("is_admin", False) or (user_ctx.get("user_role", "") == "admin")
            user_dept = user_ctx.get("department", "") or ""

            # department IS NULL/'' → org-wide, visible to all
            # :is_admin             → admin bypasses dept filter
            # department = dept     → exact dept match
            # metadata.department_ids contains dept → multi-dept PRIVATE KB
            # NOTE: no "OR :user_dept = ''" — user with no dept sees only org-wide docs
            acl_sql = """
                      AND (
                          department IS NULL
                          OR department = ''
                          OR :is_admin = true
                          OR department = :user_dept
                          OR (
                              :user_dept <> ''
                              AND jsonb_exists(metadata, 'department_ids')
                              AND jsonb_exists(metadata->'department_ids', :user_dept)
                          )
                      )"""
            acl_params = {
                "is_admin":  is_admin,
                "user_dept": user_dept,
            }

        # Phase 1 — Hard scope filter (product + domain + spec_version + doc_id)
        # Injected via user_ctx["scope_filter"] by hybrid_retriever when a KB query
        # carries explicit product/domain/version scope. Deterministic server-side
        # filter — never client-spoofable. Kills cross-product hallucination.
        # When kb_doc_id is set (user drilled to a specific document), an additional
        # metadata->>'doc_id' filter is applied so retrieval is strictly limited to
        # chunks from that document only — other docs in the same namespace are excluded.
        scope_sql    = ""
        scope_params = {}
        _scope = (user_ctx or {}).get("scope_filter", {})
        _scope_clauses = []
        if _scope:
            if _scope.get("product_id"):
                _scope_clauses.append("product_id::text = :scope_product_id")
                scope_params["scope_product_id"] = str(_scope["product_id"])
            if _scope.get("domain"):
                _scope_clauses.append("domain = :scope_domain")
                scope_params["scope_domain"] = _scope["domain"]
            if _scope.get("spec_version"):
                _scope_clauses.append("spec_version = :scope_spec_version")
                scope_params["scope_spec_version"] = _scope["spec_version"]
        # Document-level isolation: only active when the user drilled down to a
        # specific KB document. Has no effect at domain/product/version level
        # (kb_doc_id is null/absent for those scopes).
        _kb_doc_id = (user_ctx or {}).get("kb_doc_id")
        if _kb_doc_id:
            _scope_clauses.append("metadata->>'doc_id' = :scope_doc_id")
            scope_params["scope_doc_id"] = str(_kb_doc_id)
        if _scope_clauses:
            scope_sql = "AND " + " AND ".join(_scope_clauses)

        # Optional file-scope filter — graph resolver provides related file paths
        # to narrow retrieval to structurally connected components first.
        graph_sql    = ""
        graph_params = {}
        if file_filter:
            graph_sql    = "AND file_path = ANY(:file_filter)"
            graph_params = {"file_filter": file_filter}

        db = VectorSessionLocal()
        logger.info(f"TS- hybrid_search:pgvetor_search repo_scope_params-{repo_scope_params}, "
                    f"acl_params {acl_params}, scope_params={scope_params}, graph_params={graph_params}")
        # Phase 1 closure — chunk-level active-version filter (see keyword_search).
        # Exclude deprecated chunks instantly without re-indexing the prior version.
        status_sql = "AND status = 'ACTIVE'"
        try:
            rows = db.execute(
                _sql(f"""
                    SELECT id, content,
                           1 - (embedding <=> CAST(:emb AS vector)) AS similarity,
                           classification, org_id, allowed_roles, allowed_users,
                           file_path, metadata, chunk_index,
                           -- Part U11 (docx §8) hierarchy metadata for citations
                           doc_name, section_name, section_path, page_number, source_type,
                           -- Existing doc_id (in metadata JSONB)
                           metadata->>'doc_id' AS doc_id
                    FROM document_embeddings
                    WHERE {repo_scope_sql}
                      AND embedding IS NOT NULL
                    {acl_sql}
                    {scope_sql}
                    {status_sql}
                    {graph_sql}
                    ORDER BY embedding <=> CAST(:emb AS vector)
                    LIMIT :lim
                """),
                {
                    "emb":  _json.dumps(q_vec),
                    "lim":  top_k,
                    **repo_scope_params,
                    **acl_params,
                    **scope_params,
                    **graph_params,
                },
            ).fetchall()
        finally:
            db.close()

        output = []
        for row in rows:
            chunk_id, content, similarity = row[0], row[1], row[2]
            if not content:
                continue

            chunk_meta = {
                "chunk_id":     str(chunk_id),
                "classification": row[3] or "INTERNAL",
                "org_id":       row[4] or "",
                "allowed_roles": row[5] or [],
                "allowed_users": row[6] or [],
                "repo":         repo_filter,
                "file_path":    row[7] or "",
            }

            if user_ctx:
                # SQL already handled visibility/band/product — only check
                # classification/org_id/allowed_roles here.
                from core.rag_acl import check_rag_access
                granted, _ = check_rag_access(
                    user_id=user_ctx.get("user_id", ""),
                    user_role=user_ctx.get("user_role", "viewer"),
                    org_id=user_ctx.get("org_id", ""),
                    chunk_metadata=chunk_meta,
                    session_id=user_ctx.get("session_id", ""),
                    query_text=question,
                )
                if not granted:
                    continue

            row_metadata = row[8] or {}
            output.append({
                "chunk_id":     str(chunk_id),
                "text":         _search_result_content(content, row_metadata),
                "score":        max(0.0, float(similarity)),
                "source":       "pgvector",
                "repo":         repo_filter,
                "file_path":    row[7] or "",
                "metadata":     row_metadata,
                "chunk_index":  row[9],
                # Part U11 — hierarchy metadata for citation render in gateway
                "doc_name":     row[10],
                "section_name": row[11],
                "section_path": row[12],
                "page_number":  row[13],
                "source_type":  row[14],
                "doc_id":       row[15],
            })
            if len(output) >= top_k:
                break

        latency = time.time() - start
        logger.info(
            f"[kb_retrieval] pgvector search repo={repo_filter} results={len(output)} latency={latency:.3f}s"
        )
        return output

    except Exception as e:
        logger.warning(
            f"[kb_retrieval] pgvector search failed | repo={repo_filter} "
            f"error={e} elapsed={time.time() - start:.3f}s"
        )
        return []


# ============================================================
# SYMBOL SEARCH — exact lookup in code_symbols table
# ============================================================

# Regex to detect code identifiers in a query:
#   CamelCase class names, camelCase methods, snake_case names, package paths, method calls with ()
import re as _re
_SYMBOL_PATTERN = _re.compile(
    r'\b([A-Z][a-zA-Z0-9]{2,})'                      # CamelCase (class names, interfaces)
    r'|\b([a-z][a-zA-Z0-9]{2,}(?:\(\)))'             # camelCase followed by () (method calls)
    r'|\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+(?:\(\))?)'  # snake_case() or snake_case (Python/Rust/Ruby)
    r'|\b([a-z][a-zA-Z0-9]*\.[a-zA-Z][a-zA-Z0-9.]+)'  # dotted paths: org.jpos.iso.ISOMsg
)

def symbol_search(repo_filter: str, question: str, limit: int = 8) -> list[dict]:
    """
    Exact symbol lookup in the code_symbols table.

    Detects code identifiers in the question (CamelCase class names, method calls,
    package paths) and does a direct SQL lookup — no embeddings, no fuzzy matching.

    This is the highest-precision search path. Results are injected into the
    retrieval pipeline BEFORE vector search so exact symbol matches always surface.

    Returns list of dicts compatible with pgvector_search output format.
    """
    if not question or not repo_filter:
        return []

    # Extract potential symbol names from the question
    candidates: list[str] = []
    for m in _SYMBOL_PATTERN.finditer(question):
        for g in m.groups():
            if g:
                # Strip trailing () from method names
                name = g.rstrip("()")
                if len(name) >= 3:
                    candidates.append(name)

    # Also try exact words that look like identifiers (any word > 4 chars, mixed case or snake_case)
    for word in question.split():
        word_clean = word.strip(".,;:()[]{}\"'`")
        if len(word_clean) >= 4:
            # Detect CamelCase OR snake_case (contains underscore)
            has_mixed_case = any(c.isupper() for c in word_clean[1:])
            has_underscore = '_' in word_clean
            if has_mixed_case or has_underscore:
                if word_clean not in candidates:
                    candidates.append(word_clean)

    if not candidates:
        return []

    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _sql

        # Deduplicate and limit candidates to avoid excessive queries
        candidates = list(dict.fromkeys(candidates))[:10]

        repo = (repo_filter or "").lower().replace("repo_", "")

        db = SessionLocal()
        try:
            placeholders = ", ".join(f":c{i}" for i in range(len(candidates)))
            params = {"repo": repo}
            for i, c in enumerate(candidates):
                params[f"c{i}"] = c.lower()

            rows = db.execute(
                _sql(f"""
                    SELECT
                        symbol_name,
                        symbol_type,
                        file_path,
                        line_start,
                        line_end,
                        signature,
                        parent_name,
                        language,
                        embedding_id
                    FROM code_symbols
                    WHERE repo = :repo
                      AND lower(symbol_name) IN ({placeholders})
                    ORDER BY
                        CASE symbol_type
                            WHEN 'class'     THEN 1
                            WHEN 'interface' THEN 2
                            WHEN 'method'    THEN 3
                            WHEN 'function'  THEN 4
                            ELSE 5
                        END,
                        line_start
                    LIMIT :lim
                """),
                {**params, "lim": limit},
            ).fetchall()
        finally:
            db.close()

        output = []
        for row in rows:
            sym_name, sym_type, file_path, line_start, line_end, signature, parent, lang, emb_id = row

            # Format a descriptive text for the context window
            location = f"  // {file_path}:{line_start}" if file_path and line_start else ""
            parent_info = f" (in {parent})" if parent else ""
            text = (
                f"[Symbol: {sym_type} `{sym_name}`{parent_info} — {lang}]{location}\n"
                f"{signature or sym_name}"
            )

            output.append({
                "text":         text,
                "score":        1.0,   # Exact match — highest confidence
                "source":       "symbol",
                "repo":         repo_filter,
                "file_path":    file_path or "",
                "symbol_name":  sym_name,
                "symbol_type":  sym_type,
                "line_start":   line_start,
            })

        if output:
            logger.info(f"symbol_search repo={repo_filter} candidates={candidates} hits={len(output)}")
        return output

    except Exception as e:
        logger.warning(f"symbol_search failed: {e}")
        return []


# ============================================================
# REPO-LEVEL PERMISSION CHECK
# ============================================================

def check_repo_permission(repo: str, user_id: str, user_role: str) -> bool:
    """
    Check if a user may query a specific repo.

    Rules (in order):
      1. If repo_permissions table has no rows for this repo → ALLOW (default-open)
      2. If user_id is explicitly granted → ALLOW
      3. If user_role is explicitly granted → ALLOW
      4. If any explicit DENY for user_id or user_role → DENY
      5. If rows exist but none match → DENY

    admin role always bypasses permission checks.
    """
    if user_role == "admin":
        return True

    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _sql

        db = SessionLocal()
        try:
            rows = db.execute(
                _sql("""
                    SELECT user_id, user_role, granted
                    FROM repo_permissions
                    WHERE repo = :repo
                """),
                {"repo": repo},
            ).fetchall()
        finally:
            db.close()

        # No permissions configured → default open
        if not rows:
            return True

        for row_user_id, row_role, granted in rows:
            # Explicit user match
            if row_user_id and row_user_id == user_id:
                return bool(granted)
            # Role match
            if row_role and row_role == user_role:
                return bool(granted)

        # Rows exist but none matched this user → deny
        return False

    except Exception as e:
        logger.warning(f"check_repo_permission failed ({e}) — defaulting to allow")
        return True   # fail-open to prevent accidental lockout