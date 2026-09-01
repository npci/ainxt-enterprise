# SPDX-License-Identifier: Apache-2.0
"""
Hybrid retrieval pipeline — production implementation.

Pipeline (aligned with Sourcegraph architecture):

    Query
      │
      ├─ Exact symbol lookup (code_symbols table — CamelCase, method names, pkg paths)
      │
      ├─ pgvector HNSW (ANN semantic search, nomic-embed-text, 768-dim)
      │
      ├─ Postgres BM25 (tsvector/plainto_tsquery, GIN index on content)
      │
      ▼
    Merge + deduplicate (top 20 candidates)
      │
      ▼
    Pre-filter noise (LICENSE, lock files, minified assets)
      │
      ▼
    BGE reranker (bge-reranker-large, CPU, loaded at startup in embed svc)
    — cap 12 candidates in, batch_size=8, 16 OMP threads
      │
      ▼
    Symbol hits (score=1.0) prepended + Top 6 semantic chunks → LLM context

    For complex tier:
      - top_k=12 in initial pgvector retrieval (wider net)
      - Second retrieval pass with a rephrased query (query expansion)
      - All candidates merged, capped at 12, reranked down to 6

ChromaDB removed: collections were empty (0 docs). All vector data is in pgvector.
"""
import itertools
import json
import hashlib
import os
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from core.config import (
    EMBED_SVC_URL as _CORE_EMBED_SVC_URL,
    RDB_CACHE,
)
from core.kv import get_kv
import httpx

from core.logger import logger
from core.telemetry import tracer as _tracer
from models.hybrid_search import pgvector_search, keyword_search, merge_and_rerank


# ---------------------------------------------------------------------------
# Query expansion — rephrase a complex question to improve recall on the
# second retrieval pass.  Uses a lightweight LLM call via the gateway model
# router so the answer cache and compliance engine still apply.
# ---------------------------------------------------------------------------

def _expand_query(question: str) -> str:
    """
    Generate a single rephrased version of *question* for a second retrieval
    pass.  Returns the original question unchanged if expansion fails so the
    caller can always use the returned value safely.

    Routes through the LLM proxy on the LLM proxy server (LLM_PROXY_URL) — never calls
    the Anthropic SDK directly from the gateway server.
    """
    import json as _json
    import os as _os

    proxy_url = _os.getenv("LLM_PROXY_URL", "").rstrip("/")
    if not proxy_url:
        return question  # proxy not configured — skip expansion silently

    from core.model_registry import CLAUDE_HAIKU
    _system = (
        "Rephrase the following question using different wording to help "
        "retrieve relevant code and documentation chunks. Output ONLY the "
        "rephrased question — no explanation, no quotes."
    )
    combined_prompt = f"{_system}\n\nQuestion: {question}"
    try:
        tokens: list[str] = []
        from core.proxy_tool_use import llm_proxy_headers as _lph
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=3.0)) as _hc:
            with _hc.stream(
                "POST",
                f"{proxy_url}/llm/generate",
                json={"provider": "claude", "prompt": combined_prompt, "model": CLAUDE_HAIKU},
                    headers=_lph(),
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        obj = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if "t" in obj:
                        tokens.append(obj["t"])

        rephrased = "".join(tokens).strip()
        if rephrased and rephrased.lower() != question.lower():
            logger.info(f"Query expansion (proxy): '{rephrased[:80]}'")
            return rephrased
    except Exception as e:
        logger.warning(f"Query expansion (proxy) failed ({e}) — using original query")
    return question


# DB=0 cache. Backend selected via REDIS_CLIENT_CONFIG_DB0.
_redis = get_kv(RDB_CACHE, decode_responses=True)
_CACHE_TTL = 86400
_CACHE_PFX = "hybrid_retrieval:v3:"   # v3 = Phase 1 closure (status='ACTIVE' filter) — bumped so deprecated-doc cache hits don't leak

# ── Embed svc pool — round-robin across multiple instances ────────────────
# Set EMBED_SVC_URLS=http://host:8001,http://host:8002 for horizontal scale.
# Falls back to EMBED_SVC_URL (single instance) when EMBED_SVC_URLS not set.
_raw_urls = os.getenv(
    "EMBED_SVC_URLS",
    _CORE_EMBED_SVC_URL,   # single URL fallback
).split(",")
_EMBED_SVC_POOL: list = [httpx.Client(base_url=u.strip(), timeout=120.0) for u in _raw_urls if u.strip()]
_pool_cycle    = itertools.cycle(range(len(_EMBED_SVC_POOL)))
_pool_lock     = threading.Lock()

def _next_embed_client() -> httpx.Client:
    with _pool_lock:
        return _EMBED_SVC_POOL[next(_pool_cycle)]


def _rerank_via_svc(question: str, candidates: list, top_k: int = 6) -> list:
    """
    Rerank via embed svc HTTP — round-robins across pool, falls back to RRF on failure.
    """
    # Try each client in the pool before giving up
    for _ in range(len(_EMBED_SVC_POOL)):
        client = _next_embed_client()
        try:
            resp = client.post(
                "/rerank",
                json={"query": question, "candidates": candidates, "top_k": top_k},
            )
            resp.raise_for_status()
            return resp.json()["results"]
        except Exception as e:
            logger.warning(f"Rerank svc instance unavailable ({e}) — trying next in pool")
    logger.warning("All embed svc instances unavailable — RRF fallback")
    return merge_and_rerank(candidates, top_k=top_k)


def _normalize_repo_filter(repo_filter) -> list[str]:
    """
    Normalize repo_filter to a list of repo key strings.

    Accepts:
      - None / ""           → ["global"]
      - "global"            → ["global"]
      - "ainxt/payments-sdk" → ["repo_payments_sdk"]
      - ["a", "b"]          → normalized list; empty list → ["global"]

    The normalized form matches the document_embeddings.repo column format.
    """
    if isinstance(repo_filter, list):
        if not repo_filter:
            return ["global"]
        return [_normalize_single_repo(r) for r in repo_filter]
    return [_normalize_single_repo(repo_filter)]


def _normalize_single_repo(raw: str) -> str:
    """Normalize one repo string to the document_embeddings key format."""
    repo = (raw or "global").strip().lower()
    if repo and repo not in ("global",) and not repo.startswith("docs_kb:") and not repo.startswith("agent_kb:"):
        _repo_part = repo.split("/")[-1]
        _repo_part = _repo_part.replace("-", "_").replace(".", "_")
        if not _repo_part.startswith("repo_"):
            _repo_part = f"repo_{_repo_part}"
        repo = _repo_part
    return repo


def _cache_key(question: str, repo_filter, max_chunks: int = 6, file_filter=None) -> str:
    """
    Build a cache key for hybrid retrieval results.

    repo_filter may be a raw string or list. Both are normalized to the
    document_embeddings key format before hashing so:
      - ["b","a"] and ["a","b"] produce the same key (sorted)
      - "ainxt/payments-sdk" and the normalized "repo_payments_sdk" are equivalent

    file_filter (optional) — a doc allow-list restricts retrieval to specific
    file_path values, so it MUST participate in the key. Sorted before hashing so
    order does not matter. When None/empty the segment is omitted entirely, which
    keeps the key byte-for-byte identical to the pre-file_filter format for every
    existing caller (they never pass file_filter).
    """
    normalized = _normalize_repo_filter(repo_filter)
    repo_part = ",".join(sorted(normalized)) or "global"
    raw = f"{repo_part}:{max_chunks}:{(question or '').strip().lower()}"
    if file_filter:
        _ff_part = ",".join(sorted(str(f) for f in file_filter if f))
        if _ff_part:
            raw = f"{raw}:ff={_ff_part}"
    return _CACHE_PFX + hashlib.sha256(raw.encode()).hexdigest()


def _build_scope_filter(scope: dict) -> dict:
    """
    Build a validated scope filter dict from caller-supplied scope keys.
    Returns only non-None, non-empty values.
    Injected into user_ctx["scope_filter"] so pgvector_search and keyword_search
    can apply deterministic WHERE clauses before ranking.
    Supported keys: product_id (str UUID), domain (str), spec_version (str).
    """
    return {k: v for k, v in (scope or {}).items() if v}


def hybrid_retrieve_context(
    question: str,
    repo_filter,
    retriever_getter=None,        # unused — pgvector handles all retrieval
    chroma_client=None,           # unused — ChromaDB removed
    user_ctx: dict = None,
    return_confidence: bool = False,
    complexity: str = "simple",
    max_chunks: int = 6,          # IDE uses 2 for lean context; orchestrator keeps default 6
    scope: dict = None,           # Phase 1 — {"product_id": "...", "domain": "Tech", "spec_version": "v3"}
    file_filter: list = None,     # Hard allow-list of file_path values — restricts ALL retrieval passes to these docs (opt-in; None = no restriction)
) -> "list | tuple":
    """
    Returns a list of relevant text chunks (strings) for the question.
    Uses Redis cache (TTL 24h) to avoid redundant retrieval on repeated queries.

    repo_filter — scalar string OR list of strings. When a list, retrieval spans
    all listed repos in a single query (sorted comma-join for cache key).
    Empty list is treated as "global". A single string still works exactly as
    before (backward-compatible).

    user_ctx — when provided, visibility/band/product filters are injected into
    SQL WHERE so only permitted rows are fetched from the DB.  classification/
    org_id/allowed_roles are still checked as a Python post-filter via
    check_rag_access().  Callers without user_ctx get unrestricted access
    (internal pipelines, SDLC, etc.).
    Supported keys: user_id, user_role, org_id, session_id, band_level (int,
    default 1), product_ids (list[str] of UUID strings).

    return_confidence — when True, returns (context: list, confidence: float) tuple
    instead of just context list.  Backward-compatible default is False.

    complexity — "simple" | "medium" | "complex".  When "complex":
        • pgvector top_k is widened to 12 (vs default 6) for broader initial recall.
        • A second retrieval pass is performed with a rephrased query (query
          expansion) and the extra candidates are merged before the final rerank.
        • Final rerank still returns top 6 chunks so the LLM context window stays
          constant regardless of tier.

    file_filter — optional hard allow-list of ``document_embeddings.file_path``
    values. When provided, EVERY retrieval pass (primary pgvector, BM25, graph
    fallback, query-expansion, multi-query) is constrained to these paths via the
    existing ``file_filter`` arg of ``pgvector_search`` / ``keyword_search``
    (``AND file_path = ANY(:file_filter)``). This is how a caller scopes retrieval
    to a specific set of documents (e.g. Build Studio's per-workflow attached docs).
    Default ``None`` leaves behaviour unchanged for all existing callers — it is
    also folded into the Redis cache key so two otherwise-identical queries with
    different doc allow-lists never share a cache entry.
    """
    _rag_t0 = _time.perf_counter()
    try:
        return _hybrid_retrieve_context_inner(
            question=question,
            repo_filter=repo_filter,
            retriever_getter=retriever_getter,
            chroma_client=chroma_client,
            user_ctx=user_ctx,
            return_confidence=return_confidence,
            complexity=complexity,
            max_chunks=max_chunks,
            scope=scope,
            file_filter=file_filter,
        )
    finally:
        _tracer.record_rag_latency(_time.perf_counter() - _rag_t0)


def _hybrid_retrieve_context_inner(
    question: str,
    repo_filter,
    retriever_getter=None,
    chroma_client=None,
    user_ctx: dict = None,
    return_confidence: bool = False,
    complexity: str = "simple",
    max_chunks: int = 6,
    scope: dict = None,
    file_filter: list = None,
) -> "list | tuple":
    """Inner implementation — called by hybrid_retrieve_context() with latency wrapping."""
    # ── Phase 1: Hard scope filter (product + domain + spec_version) ─────────
    # Deterministic server-side filter — never client-spoofable.
    # Kills cross-product hallucination (#2 from spec architecture).
    # Injected into user_ctx so both pgvector_search and keyword_search apply
    # the same WHERE clauses before any ranking or reranking.
    if scope:
        _scope_filter = _build_scope_filter(scope)
        if _scope_filter:
            user_ctx = dict(user_ctx or {})
            user_ctx["scope_filter"] = _scope_filter
            logger.info(f"hybrid_retrieve_context: scope filter applied — {_scope_filter}")

    # Normalize to a list of repo keys for all downstream calls.
    repos: list[str] = _normalize_repo_filter(repo_filter)

    # For single-repo queries keep the existing scalar normalization path so the
    # special-namespace guards (docs_kb:*, agent_kb:*, global) still apply exactly
    # as before.  For multi-repo we pass the list directly to the search functions.
    if len(repos) == 1:
        repo = repos[0]
    else:
        repo = repos  # passed as a list to pgvector_search / keyword_search

    # ── Caller-supplied hard doc allow-list ──────────────────────────────────
    # Normalise once; None/empty ⇒ no restriction (existing callers). This is a
    # HARD constraint applied to every retrieval pass below via the existing
    # ``file_filter`` arg of pgvector_search / keyword_search. Kept distinct from
    # the graph resolver's own file_filter (a recall booster): when both are
    # present the two are intersected so the caller's allow-list always wins.
    _doc_file_filter: list = [str(f) for f in (file_filter or []) if f]

    def _combine_ff(graph_files: list = None) -> list:
        """AND the caller allow-list with a graph-scoped file list.

        • No caller filter → return graph_files unchanged (legacy behaviour).
        • Caller filter, no graph files → return the caller filter.
        • Both → intersection (never widen past the caller's hard allow-list).
        """
        if not _doc_file_filter:
            return graph_files
        if not graph_files:
            return list(_doc_file_filter)
        _allow = set(_doc_file_filter)
        return [g for g in graph_files if g in _allow]

    # Build cache key.  User-scoped queries get a per-(user, repo, question) key
    # so RBAC-filtered results are never shared across users.  TTL is 900s (15 min)
    # instead of the global 24h — shorter to respect data freshness for live repos.
    # Cross-user ACL violations are impossible because user_id is in the key.
    if user_ctx:
        _uid = (user_ctx or {}).get("user_id", "anon")
        # Delegate to _cache_key so question normalization (strip/lower) and
        # repo_filter normalization (collapse namespace prefix, sort) are inherited.
        # The "rag:" prefix keeps user-scoped keys distinguishable in Redis from
        # the global "rag:v2:" namespace used by _cache_key's _CACHE_PFX.
        cache_key = f"rag:{_uid}:" + _cache_key(question, repo_filter, max_chunks, file_filter)
    else:
        cache_key = _cache_key(question, repo_filter, max_chunks, file_filter)

    if cache_key:
        cached = _redis.get(cache_key)
        if cached:
            logger.info("Hybrid retrieval: cache hit")
            return json.loads(cached)

    try:
        is_complex = (complexity == "complex")

        from models.hybrid_search import symbol_search as _symbol_search
        # Symbol search and graph resolver operate on a single repo string.
        # For multi-repo queries use the first repo as the primary anchor for
        # symbol/graph lookups (code graph is per-repo; cross-repo graph is Phase 4).
        _sym_repo = repo[0] if isinstance(repo, list) else repo
        pgvec_top_k = 12 if is_complex else 6

        # ── Helper: safe graph resolver (never raises) ────────────────────────
        def _resolve_graph_safe(q, sym_r):
            try:
                from models.graph_resolver import resolve_graph_context
                # Thread user_ctx → the unified KG multi-hop path applies RBAC in SQL
                return resolve_graph_context(q, sym_r, user_ctx=user_ctx)
            except Exception as _ge:
                logger.debug(f"graph_resolver skipped: {_ge}")
                return [], []

        # ── ROUND 1 (parallel): symbol search + graph resolver + query expansion
        # All three are independent — fire them simultaneously.
        # query_expansion is LLM-based (2–5 s for complex tier); starting it here
        # means the wait overlaps with the DB searches in Round 2 instead of being
        # purely sequential.
        # agent_kb: repos hold document chunks only — no code symbols or call graphs,
        # so both code_symbols queries always return zero. Skip them entirely.
        # For multi-repo, skip symbol/graph if any repo is agent_kb (conservative).
        if isinstance(repo, list):
            _is_agent_kb = any(r.startswith("agent_kb:") for r in repo)
        else:
            _is_agent_kb = repo.startswith("agent_kb:")
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="retrieval-r1") as _r1:
            _fut_sym   = _r1.submit(_symbol_search, _sym_repo, question, limit=4) if not _is_agent_kb else None
            # P3: graph wiring gated by GRAPH_RETRIEVAL_ENABLED (default true for code repos)
            _graph_enabled = os.getenv("GRAPH_RETRIEVAL_ENABLED", "true").lower() in ("1", "true", "yes")
            _fut_graph = (
                _r1.submit(_resolve_graph_safe, question, _sym_repo)
                if (_sym_repo and not _is_agent_kb and _graph_enabled)
                else None
            )

            sym_results             = _fut_sym.result() if _fut_sym else []
            _graph_files, _graph_names = _fut_graph.result() if _fut_graph else ([], [])
        # Defer expansion — initialize for downstream join logic.
        expanded_query = question

        logger.info(
            f"R1 done — sym={len(sym_results)} graph_files={len(_graph_files)} "
            f"graph_names={_graph_names[:4]} (expansion deferred)"
        )

        # ── ROUND 2 (parallel): all DB/vector searches now that graph ctx is known
        # pgvec (scoped + full fallback), BM25, graph symbol boost, expansion passes
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="retrieval-r2") as _r2:
            # pgvector — graph-scoped first if we have file candidates, else full-repo
            if _graph_files:
                _fut_pgvec_graph = _r2.submit(
                    pgvector_search, repo, question,
                    top_k=pgvec_top_k, user_ctx=user_ctx, file_filter=_combine_ff(_graph_files),
                )
                _fut_pgvec_full = _r2.submit(
                    pgvector_search, repo, question,
                    top_k=max(4, pgvec_top_k // 2), user_ctx=user_ctx,
                    file_filter=_combine_ff(),
                )
            else:
                _fut_pgvec_graph = _r2.submit(
                    pgvector_search, repo, question,
                    top_k=pgvec_top_k, user_ctx=user_ctx, file_filter=_combine_ff(),
                )
                _fut_pgvec_full = None

            # BM25 — always
            _fut_bm25 = _r2.submit(keyword_search, repo, question, user_ctx=user_ctx, file_filter=_combine_ff())

            # Graph symbol boost (chunk-level precision from graph names)
            _fut_gsym = (
                _r2.submit(_symbol_search, _sym_repo, " ".join(_graph_names[:6]), limit=4)
                if _graph_names else None
            )

            # Expansion pass is deferred to ROUND 2.5 — only runs if primary
            # results are weak. Saves the 2–5 s LLM call on the common path.

            # ── Collect ──────────────────────────────────────────────────────
            pgvec = _fut_pgvec_graph.result()
            if _fut_pgvec_full:
                _pgvec_full = _fut_pgvec_full.result()
                logger.info(
                    f"pgvector graph={len(pgvec)} + full={len(_pgvec_full)} "
                    f"(graph_files={len(_graph_files)})"
                )
                pgvec = pgvec + _pgvec_full
            else:
                logger.info(f"pgvector (top_k={pgvec_top_k}): {len(pgvec)} results")

            bm25 = _fut_bm25.result()
            logger.info(f"BM25: {len(bm25)} results")

            if _fut_gsym:
                _graph_sym = _fut_gsym.result()
                _seen_fp   = {x.get("file_path") for x in sym_results}
                sym_results = sym_results + [s for s in _graph_sym if s.get("file_path") not in _seen_fp]
                logger.info(f"graph symbol boost: +{len(_graph_sym)} hits (names={_graph_names[:4]})")

        # ── Phase 2: KB parent-section expansion (always-on for docs_kb/agent_kb) ──
        # Retrieve a leaf → fetch its parent section row so the LLM sees the
        # whole section, not just a fragment. Cheap: one SQL by id ANY(...).
        if _is_agent_kb or any(r.startswith("docs_kb:") for r in (repo if isinstance(repo, list) else [repo])):
            try:
                _parent_ids = {
                    c.get("parent_chunk_id") for c in pgvec
                    if c.get("parent_chunk_id") and not c.get("is_section_parent")
                }
                if _parent_ids:
                    from db.database import VectorSessionLocal as _VSL
                    from sqlalchemy import text as _sqlt
                    _have_ids = {c.get("chunk_id") for c in pgvec}
                    _missing  = [pid for pid in _parent_ids if pid not in _have_ids]
                    if _missing:
                        _vdb = _VSL()
                        try:
                            _rows = _vdb.execute(
                                _sqlt(
                                    "SELECT id, content, file_path, section_path "
                                    "FROM document_embeddings WHERE id = ANY(:ids)"
                                ),
                                {"ids": _missing},
                            ).fetchall()
                        finally:
                            _vdb.close()
                        _expansion = []
                        for _r in _rows:
                            _expansion.append({
                                "chunk_id":  str(_r[0]),
                                "text":      _r[1] or "",
                                "file_path": _r[2] or "",
                                "score":     0.85,   # boost — whole-section parents are high-signal
                                "is_section_parent": True,
                                "section_path": _r[3] or "",
                            })
                        pgvec = pgvec + _expansion
                        logger.info(
                            f"KB parent expansion: +{len(_expansion)} parent section(s) from "
                            f"{len(_parent_ids)} leaf parent_chunk_id refs"
                        )
            except Exception as _pe:
                logger.warning(f"KB parent expansion failed (non-fatal): {_pe}")

        # ── P3: Hierarchical retrieval for code repos (parent_chunk_id) ─────────
        # For code repo chunks that have a parent_chunk_id, fetch the parent row
        # to give the LLM broader context (e.g. the full function/class containing
        # a matched line). Mirrors the KB parent expansion above but for repo_* namespaces.
        _is_code_repo = not _is_agent_kb and not any(
            (r.startswith("docs_kb:") or r.startswith("agent_kb:"))
            for r in (repo if isinstance(repo, list) else [repo])
        )
        if _is_code_repo:
            try:
                _code_parent_ids = {
                    c.get("parent_chunk_id") for c in pgvec
                    if c.get("parent_chunk_id") and not c.get("is_section_parent")
                }
                if _code_parent_ids:
                    from db.database import VectorSessionLocal as _VSL_code
                    from sqlalchemy import text as _sqlt_code
                    _have_code_ids = {c.get("chunk_id") for c in pgvec}
                    _missing_code = [pid for pid in _code_parent_ids if pid not in _have_code_ids]
                    if _missing_code:
                        _vdb_code = _VSL_code()
                        try:
                            _code_rows = _vdb_code.execute(
                                _sqlt_code(
                                    "SELECT id, content, file_path, section_path "
                                    "FROM document_embeddings WHERE id = ANY(:ids)"
                                ),
                                {"ids": _missing_code},
                            ).fetchall()
                        finally:
                            _vdb_code.close()
                        _code_expansion = []
                        for _cr in _code_rows:
                            _code_expansion.append({
                                "chunk_id":          str(_cr[0]),
                                "text":              _cr[1] or "",
                                "file_path":         _cr[2] or "",
                                "score":             0.80,   # parent context boost
                                "is_section_parent": True,
                                "section_path":      _cr[3] or "",
                            })
                        pgvec = pgvec + _code_expansion
                        logger.info(
                            f"Code parent expansion: +{len(_code_expansion)} parent chunk(s) from "
                            f"{len(_code_parent_ids)} leaf parent_chunk_id refs"
                        )
            except Exception as _cpe:
                logger.warning(f"Code parent expansion failed (non-fatal): {_cpe}")

        # ── ROUND 2.5: deferred query expansion (LLM-based, 2–5 s) ───────────
        # Fired only when primary retrieval looks weak: complex tier + no symbol
        # exact match + top pgvector score below the confidence floor. This
        # eliminates the unconditional 2–5 s LLM call on every complex query
        # (the most common latency complaint) while preserving recall on the
        # genuinely hard queries where rephrasing actually helps.
        _CONF_FLOOR = 0.55
        _top_primary_score = max(
            (r.get("score", 0) for r in pgvec),
            default=0.0,
        )
        expansion_candidates: list = []
        if (
                is_complex
                and not sym_results
                and _top_primary_score < _CONF_FLOOR
        ):
            try:
                expanded_query = _expand_query(question) or question
            except Exception as _exp_err:
                logger.debug(f"query expansion failed (proceeding without): {_exp_err}")
                expanded_query = question
            if expanded_query and expanded_query != question:
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="retrieval-r25") as _r25:
                    _fut_exp_pgvec = _r25.submit(
                        pgvector_search, repo, expanded_query,
                        top_k=pgvec_top_k, user_ctx=user_ctx, file_filter=_combine_ff(),
                    )
                    _fut_exp_bm25 = _r25.submit(keyword_search, repo, expanded_query, user_ctx=user_ctx, file_filter=_combine_ff())
                    _exp_pgvec = _fut_exp_pgvec.result()
                    _exp_bm25  = _fut_exp_bm25.result()
                expansion_candidates = _exp_pgvec + _exp_bm25
                logger.info(
                    f"Query expansion: top_primary={_top_primary_score:.3f} < {_CONF_FLOOR} → "
                    f"+{len(expansion_candidates)} additional candidates from rephrased query"
                )
            else:
                logger.info(
                    f"Query expansion skipped: rephrase returned same query"
                )
        else:
            logger.info(
                f"Query expansion gated: complex={is_complex} sym_hit={bool(sym_results)} "
                f"top_score={_top_primary_score:.3f}"
            )


        # ── P3: Multi-query decomposition (complex queries with conjunctions) ───
        # For complex-tier queries that contain "and/or/also/as well as" and are
        # longer than 15 words, decompose into sub-queries and run parallel retrieval.
        # Each sub-query retrieves independently; results are merged before reranking.
        # Gated: only fires when is_complex=True and query has conjunction signals.
        multi_query_candidates: list = []
        _MULTI_QUERY_MIN_WORDS = 15
        _conjunction_re_pat = r'\b(and|or|also|as well as|additionally|plus|both)\b'
        import re as _re_mq
        _has_conjunction = bool(_re_mq.search(_conjunction_re_pat, question, _re_mq.IGNORECASE))
        _word_count = len(question.split())
        if is_complex and _has_conjunction and _word_count >= _MULTI_QUERY_MIN_WORDS:
            try:
                def _decompose_query(q: str) -> list:
                    """Split a compound question into 2–3 focused sub-queries via LLM."""
                    import json as _jq
                    proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")
                    if not proxy_url:
                        return []
                    from core.model_registry import CLAUDE_HAIKU
                    _sys = (
                        "Split the following compound question into 2-3 focused sub-questions. "
                        "Output ONLY a JSON array of strings, e.g. [\"sub-q1\", \"sub-q2\"]. "
                        "No explanation, no markdown."
                    )
                    _prompt = f"{_sys}\n\nQuestion: {q}"
                    try:
                        from core.proxy_tool_use import llm_proxy_headers as _lph
                        with httpx.Client(timeout=httpx.Timeout(12.0, connect=3.0)) as _hc:
                            resp = _hc.post(
                                f"{proxy_url}/llm/generate",
                                json={"provider": "claude", "prompt": _prompt, "model": CLAUDE_HAIKU},
                                headers=_lph(),
                            )
                            resp.raise_for_status()
                            raw = resp.json().get("text") or resp.text
                            # Extract JSON array from response
                            _m = _re_mq.search(r'\[.*?\]', raw, _re_mq.DOTALL)
                            if _m:
                                subs = _jq.loads(_m.group())
                                return [s.strip() for s in subs if isinstance(s, str) and s.strip()]
                    except Exception as _dqe:
                        logger.debug(f"query decomposition failed: {_dqe}")
                    return []

                sub_queries = _decompose_query(question)
                if len(sub_queries) >= 2:
                    logger.info(f"Multi-query decomposition: {len(sub_queries)} sub-queries from complex question")

                    def _retrieve_raw(q: str) -> list:
                        try:
                            _pv = pgvector_search(repo, q, top_k=4, user_ctx=user_ctx, file_filter=_combine_ff())
                            _bm = keyword_search(repo, q, user_ctx=user_ctx, file_filter=_combine_ff())
                            return _pv + _bm
                        except Exception:
                            return []

                    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="retrieval-mq") as _mq:
                        _sub_results = list(_mq.map(_retrieve_raw, sub_queries))
                    multi_query_candidates = [c for r in _sub_results for c in r]
                    logger.info(f"Multi-query: +{len(multi_query_candidates)} candidates from {len(sub_queries)} sub-queries")
            except Exception as _mqe:
                logger.warning(f"Multi-query decomposition skipped (non-fatal): {_mqe}")

        # ── STEP 3: Merge + deduplicate (max score wins per chunk) ────────────
        # Symbol results are kept separate — they are not reranked (already exact).
        all_candidates = pgvec + bm25 + expansion_candidates + multi_query_candidates
        merge_top_k = 40 if is_complex else 10  # reduced from 20: fewer candidates → faster reranker HTTP call
        merged = merge_and_rerank(all_candidates, top_k=merge_top_k)
        logger.info(f"Merged: {len(merged)} unique candidates (complex={is_complex})")

        # ── STEP 4: Rerank via embed svc (BGE reranker, runs in embed svc process) ──
        # Final output capped to max_chunks so IDE callers (max_chunks=2) get lean context.
        reranked = _rerank_via_svc(question, merged, top_k=max_chunks)
        logger.info(f"Reranked: {len(reranked)} final chunks (max_chunks={max_chunks})")

        # ── P6: Feedback-driven chunk quality penalty ─────────────────────────────
        # Apply penalty scores from thumbs-down feedback (computed by FeedbackProcessor).
        # Cold-start guard: get_chunk_quality_score() returns 1.0 when no data.
        # Only applied when FEEDBACK_PENALTY_ENABLED=true (default true).
        if os.getenv("FEEDBACK_PENALTY_ENABLED", "true").lower() in ("1", "true", "yes"):
            try:
                from services.feedback_processor import get_chunk_quality_score as _gqs
                _penalized = 0
                for _chunk in reranked:
                    _cid = _chunk.get("chunk_id") or _chunk.get("id") or ""
                    _penalty = _gqs(str(_cid))
                    if _penalty < 1.0:
                        _chunk["score"] = float(_chunk.get("score", 0.5)) * _penalty
                        _penalized += 1
                if _penalized:
                    reranked.sort(key=lambda c: c.get("score", 0), reverse=True)
                    logger.info(f"Feedback penalty: applied to {_penalized} chunks")
            except Exception as _fpe:
                logger.debug(f"Feedback penalty skipped (non-fatal): {_fpe}")

        # ── STEP 5: Relevance gate — drop reranked chunks that score below the BGE
        # threshold.  BGE reranker returns sigmoid-normalised scores in [0,1].
        # Scores below 0.30 indicate the chunk is not meaningfully related to the
        # question; keeping them causes the LLM to produce hallucinated responses.
        # Env override: RERANKER_MIN_SCORE (float, default 0.30).
        _RERANKER_MIN_SCORE = float(os.getenv("RERANKER_MIN_SCORE", "0.30"))
        high_quality = [r for r in reranked if float(r.get("score", 0)) >= _RERANKER_MIN_SCORE]
        if len(high_quality) < len(reranked):
            logger.info(
                f"Relevance gate: dropped {len(reranked) - len(high_quality)} low-score chunks "
                f"(threshold={_RERANKER_MIN_SCORE})"
            )
        reranked = high_quality

        # ── Phase 3: Coverage tier dispatch (§8y gate + retrieval_scope knob) ──
        # Operator config KB_RETRIEVAL_SCOPE controls how Fast + Coverage
        # combine for a KB query that carries explicit scope:
        #
        #   "auto"      — Fast first, escalate to Coverage only if the gate
        #                 fires (default, kn_rewrite.md §8y).
        #   "rag"       — Fast only; skip Coverage entirely.
        #   "full_file" — Coverage only; discard Fast hits and force
        #                 coverage_retriever to read every section.
        #   "both"      — Run Coverage unconditionally, then CONCAT Fast +
        #                 Coverage and RE-RANK the combined list via embed_svc
        #                 /rerank, keeping the global top-k.
        #
        # Code/agent_kb queries (no scope) bypass this block — they end at
        # the Fast tier regardless of mode.
        coverage_evidence: list[dict] = []
        coverage_badge:   str          = ""
        coverage_trace:   dict         = {}
        try:
            from core.config import KB_RETRIEVAL_SCOPE as _RS
            _scope_for_coverage = (user_ctx or {}).get("scope_filter") or {}
            _coverage_doc_id    = (user_ctx or {}).get("kb_doc_id")
            _coverage_enabled   = os.getenv("KB_COVERAGE_ENABLED", "true").lower() in ("1", "true", "yes")
            _has_kb_scope = bool(
                _scope_for_coverage.get("product_id")
                and _scope_for_coverage.get("spec_version")
            )
            # Include domain-only scope so Coverage Tier runs when the user
            # chats at domain level without a product_id + spec_version pair.
            # Previously domain-only chats always fell through to Fast-tier-only
            # because _has_any_scope was False — this was the root cause of
            # incomplete answers at domain level.
            _has_any_scope = (
                _has_kb_scope
                or bool(_coverage_doc_id)
                or bool(_scope_for_coverage.get("domain"))
            )

            # Effective-scope rule (graph selection depth):
            #   • User selected a specific document node → kb_doc_id is set
            #     → always use "full_file" (verbatim whole-doc evidence),
            #     regardless of the KB_RETRIEVAL_SCOPE env var.
            #   • User selected domain / product / version (no doc)
            #     → kb_doc_id is null → use KB_RETRIEVAL_SCOPE as configured
            #     (default "both").
            _RS_effective = "full_file" if _coverage_doc_id else _RS

            # Mode == "rag" short-circuits the whole block.
            if _RS_effective == "rag":
                coverage_trace = {
                    "retrieval_scope":   "rag",
                    "mode":              "fast",
                    "escalate":          False,
                    "sections_examined": 0,
                    "sections_included": 0,
                    "badge":             "RAG only (KB_RETRIEVAL_SCOPE=rag)",
                }
                if isinstance(user_ctx, dict):
                    user_ctx["_coverage_trace_out"] = coverage_trace
            # Mode == "full_file" requires explicit scope — without it we
            # refuse rather than scan everything (silent over-fetch would
            # be worse than no answer).
            elif _RS_effective == "full_file" and not _has_any_scope:
                coverage_trace = {
                    "retrieval_scope":   "full_file",
                    "mode":              "skipped",
                    "escalate":          False,
                    "reason":            "no_scope_set — full_file mode requires kb_doc_id or (product_id, spec_version)",
                    "sections_examined": 0,
                    "sections_included": 0,
                    "badge":             "Full-file mode requires a scoped chat",
                }
                if isinstance(user_ctx, dict):
                    user_ctx["_coverage_trace_out"] = coverage_trace
                logger.warning(
                    "KB_RETRIEVAL_SCOPE=full_file but the chat has no scope; "
                    "skipping Coverage (no over-fetch). Fast hits are returned."
                )
            elif _RS_effective in ("auto", "both", "full_file") and not _coverage_enabled:
                logger.info(
                    f"[KB_COVERAGE] disabled by KB_COVERAGE_ENABLED=false — "
                    f"effective_mode={_RS_effective} configured_mode={_RS} returning Fast hits only"
                )
            elif _RS_effective in ("auto", "both") and not _has_any_scope:
                logger.info(
                    f"[KB_COVERAGE] skipped — no scope on chat "
                    f"(needs product_id+spec_version or kb_doc_id). "
                    f"effective_mode={_RS_effective} configured_mode={_RS}"
                )
            elif _coverage_enabled and _has_any_scope:
                from models.coverage_gate import evaluate as _gate_eval
                from store import kb_doc_cache as _kb_cache
                from models.coverage_retriever import run_coverage as _run_coverage

                logger.info(
                    f"[KB_COVERAGE] entering coverage path — "
                    f"effective_mode={_RS_effective} configured_mode={_RS} "
                    f"explicit_doc_id={_coverage_doc_id} has_kb_scope={_has_kb_scope}"
                )

                # Resolve the doc to inspect. Explicit doc_id wins; otherwise pick
                # the most-cited doc from the Fast hits (their `file_path` for KB
                # docs is the original filename — the canonical doc_id is in the
                # chunk metadata, which is fetched lazily here only if needed).
                _doc_id = _coverage_doc_id
                if not _doc_id and reranked:
                    # Pull doc_id from any retrieved chunk metadata. Cheap SQL lookup.
                    try:
                        from db.database import VectorReadSessionLocal as _VRS
                        from sqlalchemy import text as _sqlt
                        _vdb = _VRS()
                        try:
                            _rid = reranked[0].get("chunk_id")
                            if _rid:
                                _row = _vdb.execute(
                                    _sqlt(
                                        "SELECT metadata->>'doc_id' FROM document_embeddings "
                                        "WHERE id = :cid"
                                    ),
                                    {"cid": _rid},
                                ).fetchone()
                                if _row and _row[0]:
                                    _doc_id = _row[0]
                        finally:
                            _vdb.close()
                    except Exception as _did_e:
                        logger.debug(f"coverage doc_id lookup skipped: {_did_e}")

                if not _doc_id:
                    # Coverage needs a doc_id — without one it can't load the
                    # section_map. This is the most common silent-skip in
                    # production: the chat was confirmed at the version/product
                    # level (no document picked) and Fast tier returned no hits
                    # to back-derive a doc_id from.
                    logger.info(
                        f"[KB_COVERAGE] skipped — no doc_id resolved "
                        f"(explicit kb_doc_id missing AND no reranked chunks to back-derive). "
                        f"effective_mode={_RS_effective} configured_mode={_RS} "
                        f"scope={_scope_for_coverage}"
                    )
                if _doc_id:
                    _payload = _kb_cache.get_or_warm(
                        _scope_for_coverage.get("product_id"),
                        _scope_for_coverage.get("spec_version"),
                        _doc_id,
                    )
                    if not _payload:
                        logger.info(
                            f"[KB_COVERAGE] skipped — kb_doc_cache miss for "
                            f"doc_id={_doc_id} product_id={_scope_for_coverage.get('product_id')} "
                            f"spec_version={_scope_for_coverage.get('spec_version')}"
                        )
                    if _payload:
                        _section_paths = {
                            r.get("section_path") for r in reranked if r.get("section_path")
                        }
                        # Phase 5: dependency-leak signal via entity edges.
                        # Tells the gate "the retrieved chunks point at chunks
                        # the Fast tier missed" → escalate.
                        try:
                            from models.kb_graph_expand import has_dependency_leak as _has_leak
                            _retrieved_ids = [r.get("chunk_id") for r in reranked if r.get("chunk_id")]
                            _leak = _has_leak(
                                _retrieved_ids,
                                _scope_for_coverage.get("product_id"),
                                _scope_for_coverage.get("spec_version"),
                            )
                        except Exception:
                            _leak = False

                        # Gate evaluation — used by 'auto' for the escalation
                        # decision and by 'both'/'full_file' purely as
                        # observability (we run Coverage either way).
                        _gate = _gate_eval(
                            question=question,
                            reranked=reranked,
                            section_map=_payload.get("section_map") or [],
                            parent_section_paths=_section_paths,
                            has_dependency_leak=_leak,
                        )
                        coverage_trace = {
                            "retrieval_scope":  _RS_effective,
                            "configured_scope": _RS,
                            "escalate":         _gate.escalate,
                            "sufficiency":      _gate.sufficiency,
                            "reason":           _gate.reason,
                            "signals":          _gate.signals,
                        }

                        # Decide whether Coverage runs for THIS request.
                        # auto      → only when gate escalates
                        # both      → always
                        # full_file → always (also triggered when kb_doc_id is
                        #             set, because _RS_effective is forced to
                        #             "full_file" in that case regardless of the
                        #             KB_RETRIEVAL_SCOPE env var).
                        _run_cov = (
                            _RS_effective in ("both", "full_file")
                            or (_RS_effective == "auto" and _gate.escalate)
                        )

                        if _run_cov:
                            _why = (
                                f"effective_mode={_RS_effective} configured_mode={_RS}"
                                if _RS_effective != "auto"
                                else f"gate_escalate sufficiency={_gate.sufficiency:.2f} "
                                     f"reason='{_gate.reason}'"
                            )
                            logger.info(f"[KB_COVERAGE] running coverage — {_why} doc_id={_doc_id}")
                            _cov = _run_coverage(question, _payload, reranked, retrieval_scope=_RS_effective)
                            coverage_badge = _cov.badge
                            coverage_trace.update({
                                "mode":               _cov.mode,
                                "sections_examined":  _cov.sections_examined,
                                "sections_included":  _cov.sections_included,
                                "badge":              _cov.badge,
                                "coverage_trace":     _cov.trace,
                            })

                            if _RS_effective == "full_file":
                                # Full-file mode: Coverage IS the answer. Drop
                                # Fast hits so they don't dilute the verbatim
                                # whole-section evidence the reasoner gets.
                                # Safety guard: if coverage returned empty evidence
                                # (e.g. semaphore timeout → map_reduce_skipped),
                                # keep fast hits rather than leaving the LLM with
                                # zero context.
                                if _cov.evidence:
                                    coverage_evidence = _cov.evidence
                                    reranked = []
                                else:
                                    logger.warning(
                                        f"[KB_COVERAGE] full_file coverage returned no evidence "
                                        f"(mode={_cov.mode}) — retaining fast hits as fallback"
                                    )
                                    coverage_evidence = []
                                    # reranked is kept as-is (fast hits preserved)
                                coverage_trace["badge"] = (
                                    f"Full-file: {coverage_badge}"
                                )
                            elif _RS_effective == "both":
                                # Both mode: merge Fast hits + Coverage sections
                                # into a single candidate pool, re-rank the
                                # combined list via embed_svc, keep top-k overall.
                                # Coverage sections carry a `text` field already;
                                # we adapt their shape into rerank candidates.
                                _cov_candidates = [
                                    {
                                        "text":         e.get("text") or "",
                                        "section_path": e.get("section_path"),
                                        "doc_id":       e.get("doc_id"),
                                        "source":       "coverage",
                                        "score":        e.get("score") or 0.0,
                                    }
                                    for e in _cov.evidence
                                    if (e.get("text") or "").strip()
                                ]
                                # Tag fast-tier hits so the merged list can be
                                # rendered with source attribution later.
                                _fast_candidates = [
                                    {**r, "source": r.get("source") or "fast"}
                                    for r in reranked
                                ]
                                _combined = _fast_candidates + _cov_candidates
                                if _combined:
                                    _merged_topk = _rerank_via_svc(
                                        question, _combined, top_k=max_chunks
                                    )
                                    # Split back into the two carriers the
                                    # downstream code expects: chunks that came
                                    # from Coverage become `coverage_evidence`
                                    # (verbatim, prepended), chunks from Fast
                                    # stay in `reranked`. Order preserved.
                                    coverage_evidence = [
                                        m for m in _merged_topk
                                        if m.get("source") == "coverage"
                                    ]
                                    reranked = [
                                        m for m in _merged_topk
                                        if m.get("source") != "coverage"
                                    ]
                                    coverage_trace["merged_top_k"] = len(_merged_topk)
                                    coverage_trace["fast_kept"]    = len(reranked)
                                    coverage_trace["cov_kept"]     = len(coverage_evidence)
                                    coverage_trace["badge"] = (
                                        f"Both (Fast+Coverage reranked): {coverage_badge}"
                                    )
                            else:
                                # auto mode w/ gate escalation — original behavior.
                                coverage_evidence = _cov.evidence
                        else:
                            # auto mode, gate did not escalate.
                            coverage_badge = (
                                f"Fast tier sufficient ({_gate.sufficiency:.2f})"
                            )
                            coverage_trace.update({
                                "mode":              "fast",
                                "sections_examined": 0,
                                "sections_included": 0,
                                "badge":             coverage_badge,
                            })
                        # Publish the trace onto user_ctx so the gateway can
                        # forward it via the SSE __meta__ frame and the Chat
                        # UI can render a coverage badge under the answer
                        # (kn_rewrite.md §8x — Transparency surfaced to user).
                        if isinstance(user_ctx, dict):
                            user_ctx["_coverage_trace_out"] = coverage_trace
        except Exception as _ce:
            logger.warning(f"Coverage escalation skipped (non-fatal): {_ce}")

        # ── Part U14 (docx §11) — graph traversal for impact / lineage ──────
        # docx §11 "Where Graph Helps": questions like "Why was this introduced?",
        # "What changed because of this?", "Which documents are affected?",
        # "What decisions are connected?" — these are answered by walking the
        # TYPED dependency edges (approved_by / implements / governed_by /
        # references / supersedes) written at index time by kb_entity_worker's
        # _scan_relations pass.
        #
        # Trigger: caller's question matches an impact/lineage intent pattern
        # AND we have a top KB doc to walk from. Pattern is conservative — it
        # only fires on natural-language impact phrasing so a plain "what is X"
        # question is unaffected.
        graph_evidence: list[dict] = []
        try:
            import re as _re_imp
            _impact_re = _re_imp.compile(
                r'\b(why|impact|affected|affect|what\s+changed|introduce[ds]?|'
                r'depend(s|ent|encies|encies?)|govern(s|ed)?|supersede|'
                r'replace[ds]?|connect(s|ed)?)\b',
                _re_imp.IGNORECASE
            )
            if (question and _impact_re.search(question) and reranked
                    and (user_ctx or {}).get("scope_filter")):
                from models.kb_graph_expand import neighbors_for_doc as _nd
                # Resolve the top reranked chunk's doc_id (cheap SQL lookup,
                # mirrors the coverage-tier doc_id resolution above).
                _top_chunk_id = reranked[0].get("chunk_id")
                _top_doc_id: str | None = None
                if _top_chunk_id:
                    try:
                        from db.database import VectorReadSessionLocal as _VRS_imp
                        from sqlalchemy import text as _sqlt_imp
                        _vdb_i = _VRS_imp()
                        try:
                            _ridr = _vdb_i.execute(_sqlt_imp(
                                "SELECT metadata->>'doc_id' FROM document_embeddings "
                                "WHERE id = :cid"
                            ), {"cid": _top_chunk_id}).fetchone()
                            if _ridr and _ridr[0]:
                                _top_doc_id = _ridr[0]
                        finally:
                            _vdb_i.close()
                    except Exception as _de:
                        logger.debug(f"impact-query doc_id lookup skipped: {_de}")

                if _top_doc_id:
                    _sf = (user_ctx or {}).get("scope_filter") or {}
                    graph_evidence = _nd(
                        doc_id=_top_doc_id,
                        product_id=_sf.get("product_id"),
                        spec_version=_sf.get("spec_version"),
                        max_hops=2,
                        limit=8,
                    )
                    if graph_evidence:
                        # Publish to coverage_trace so the UI badge can render
                        # the lineage chain (kn_rewrite.md §8x transparency).
                        coverage_trace = coverage_trace or {}
                        coverage_trace["graph_walk"] = [
                            {
                                "dst_doc_id":   g["dst_doc_id"],
                                "dst_doc_name": g["dst_doc_name"],
                                "source_type":  g.get("source_type"),
                                "relation":     g["relation"],
                                "hops":         g["hops"],
                            }
                            for g in graph_evidence
                        ]
                        if isinstance(user_ctx, dict):
                            user_ctx["_coverage_trace_out"] = coverage_trace
                        logger.info(
                            f"[KB_GRAPH] impact-query walked from doc {_top_doc_id} → "
                            f"{len(graph_evidence)} typed edges "
                            f"(relations={sorted({g['relation'] for g in graph_evidence})})"
                        )
        except Exception as _ge:
            logger.warning(f"Impact-query graph walk skipped (non-fatal): {_ge}")

        # Prepend file path header so the LLM can cite sources accurately.
        # Symbol hits come first (exact match), then reranked semantic results.
        # Cap symbol hits so they don't crowd out semantic results when max_chunks is small.
        _sym_cap = max(1, max_chunks - len(reranked))
        context = []

        # Symbol results first — exact matches go to top of context window
        for item in sym_results[:_sym_cap]:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            fp   = item.get("file_path", "")
            text = item["text"][:1500]
            context.append(f"[Source: {fp}]\n{text}" if fp else text)

        # Then semantic + BM25 results
        for item in reranked:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            fp   = item.get("file_path", "")
            text = item["text"][:1500]
            context.append(f"[Source: {fp}]\n{text}" if fp else text)

        # Phase 3: Coverage evidence prepends VERBATIM section text when the gate
        # escalated. Each item carries the doc + section_path so the LLM can cite.
        # No dedup, no compression — §8z forbids touching evidence path.
        if coverage_evidence:
            _cov_prefix = []
            for _ce_item in coverage_evidence:
                _sp = _ce_item.get("section_path") or ""
                _txt = _ce_item.get("text") or ""
                if not _txt:
                    continue
                _hdr = f"[Coverage source: {_sp}]" if _sp else "[Coverage source]"
                _cov_prefix.append(f"{_hdr}\n{_txt}")
            # Coverage evidence goes BEFORE Fast-tier hits so the reasoner reads
            # the whole-section verbatim text first.
            context = _cov_prefix + context
            logger.info(
                f"[KB_COVERAGE] merged {len(_cov_prefix)} coverage section(s) into context "
                f"(badge={coverage_badge!r})"
            )

        # ── Part U14 — graph-walk evidence (impact / lineage) ───────────────
        # Lineage evidence carries the relation chain (e.g. "approved_by TPMC
        # Decision 2024-08-15"). Goes BEFORE Fast hits but AFTER Coverage so the
        # reasoner sees: whole-section text first, lineage second, fragments
        # last. Tagged "[Lineage source: ...]" so trim_rag_chunk skips it.
        if graph_evidence:
            _graph_prefix: list[str] = []
            for _g in graph_evidence:
                _ev = (_g.get("evidence") or "").strip()
                _rel = _g.get("relation") or ""
                _dst = _g.get("dst_doc_name") or _g.get("dst_doc_id") or "(unknown)"
                _st  = _g.get("source_type") or ""
                _hdr = f"[Lineage source: {_rel} → {_dst}"
                if _st:
                    _hdr += f" ({_st})"
                _hdr += "]"
                _body = _ev or f"(graph edge — no captured evidence sentence)"
                _graph_prefix.append(f"{_hdr}\n{_body}")
            context = _graph_prefix + context
            logger.info(
                f"[KB_GRAPH] merged {len(_graph_prefix)} lineage edge(s) into context "
                f"(top relation chain: "
                f"{[g.get('relation') for g in graph_evidence[:3]]})"
            )

        # Phase 1: Dedup same-source chunks then trim each chunk from 1500 → 800 chars.
        # Dedup keeps first occurrence (highest-scored by reranker).
        # Trim uses head+tail strategy — never drops unique file sections.
        from core.context_compressor import dedup_rag_chunks, trim_rag_chunk

        # ── Compression diagnostics ───────────────────────────────────────────
        _before_dedup_count = len(context)
        _before_rag_chars   = sum(len(c) for c in context)

        context = dedup_rag_chunks(context)
        _after_dedup_count = len(context)
        _after_dedup_chars = sum(len(c) for c in context)

        _pre_trim_chunks = list(context)  # snapshot for per-chunk diff below
        # Phase 3 + Part U14: Coverage-source AND Lineage-source rows MUST
        # stay verbatim (no trim, no LLMLingua). The [Coverage source: …] and
        # [Lineage source: …] headers are the markers; everything else is the
        # Fast-tier RAG payload that we can safely compress.
        context = [
            c if (c.startswith("[Coverage source") or c.startswith("[Lineage source"))
            else trim_rag_chunk(c, max_chars=800)
            for c in context
        ]
        _after_rag_chars = sum(len(c) for c in context)

        # Per-chunk trim diff — shows exactly what changed (or didn't)
        for _i, (_before_c, _after_c) in enumerate(zip(_pre_trim_chunks, context)):
            _trimmed = len(_before_c) != len(_after_c)

        _total_reduction = _before_rag_chars - _after_rag_chars
        _reduction_pct   = 100 * _total_reduction / max(_before_rag_chars, 1)

        try:
            from core.compress_metrics import record as _cmr
            _cmr("rag_phase1", _before_rag_chars, _after_rag_chars)
        except Exception:
            pass

        # Phase 3+4: LLMLingua-2 for prose namespaces (Confluence/platform docs).
        # Only enabled when ENABLE_LINGUA_COMPRESS=true AND namespace is prose.
        # Never applied to repo_* (code) namespaces — risk of dropping critical tokens.
        # For list repo_filter, compression only fires when ALL repos are prose
        # namespaces (i.e. the first normalized key passes the namespace gate).
        # Multi-repo code contexts are always excluded by the repo_* prefix check.
        _lingua_ns = repos[0] if repos else ""
        # Phase 3: split off Coverage rows so LLMLingua never touches verbatim
        # spec evidence. Compress only the Fast-tier rows, then restitch.
        _cov_rows = [c for c in context if c.startswith("[Coverage source")]
        _fast_rows = [c for c in context if not c.startswith("[Coverage source")]
        _fast_rows = _lingua_compress_if_enabled(_fast_rows, question, _lingua_ns)
        context = _cov_rows + _fast_rows

        # ── P3: Lost-in-the-middle mitigation ────────────────────────────────────
        # LLMs attend best to content at the start and end of the context window.
        # Reorder so the highest-scored chunk is first and the second-best is last;
        # lower-scored chunks fill the middle. Only applied to Fast-tier rows
        # (Coverage and Lineage rows are verbatim and must stay at the front).
        # Reference: Liu et al. 2023 "Lost in the Middle".
        def _reorder_for_attention(chunks: list) -> list:
            if len(chunks) <= 2:
                return chunks
            # Separate special-prefix rows (Coverage/Lineage) from Fast-tier rows
            _special = [c for c in chunks if c.startswith("[Coverage source") or c.startswith("[Lineage source")]
            _fast = [c for c in chunks if not (c.startswith("[Coverage source") or c.startswith("[Lineage source"))]
            if len(_fast) <= 2:
                return chunks
            # Place best at index 0, second-best at index -1, rest in middle
            _best = _fast[0]
            _second = _fast[1]
            _middle = _fast[2:]
            _reordered_fast = [_best] + _middle + [_second]
            return _special + _reordered_fast

        context = _reorder_for_attention(context)

        # Compute confidence score based on result quality
        # Symbol hit = 0.95 (exact), good semantic = 0.85, few results = lower
        if sym_results:
            _confidence = 0.95
        elif len(reranked) >= 4:
            top_score = max((r.get("score", 0) for r in reranked), default=0)
            _confidence = min(0.90, 0.5 + top_score * 0.4)
        elif len(reranked) >= 1:
            _confidence = 0.55
        else:
            _confidence = 0.0

        # Eval hook (fire-and-forget, non-blocking)
        try:
            from core.evals import eval_engine
            eval_engine.eval_retrieval_quality(question, context)
        except Exception:
            pass

        # Phase 3: surface coverage trace into Redis db=1 so TracePanel can
        # render the auditable badge ("Read all 312 pages" vs "6 sections").
        if coverage_trace:
            try:
                from core.trace_store import add_trace as _add_trace
                _req_id = (user_ctx or {}).get("request_id") or (user_ctx or {}).get("session_id")
                if _req_id:
                    _msg = (
                        f"kb_coverage badge={coverage_badge!r} "
                        f"escalate={coverage_trace.get('escalate')} "
                        f"sufficiency={coverage_trace.get('sufficiency')} "
                        f"reason={coverage_trace.get('reason')!r} "
                        f"signals={json.dumps(coverage_trace.get('signals', {}))}"
                    )
                    _add_trace(_req_id, _msg)
            except Exception:
                # Trace path is best-effort — never break retrieval on it.
                pass

        # Phase 3: never cache results that include Coverage-tier evidence —
        # the payload is large and the gate decision depends on live signals.
        # Fast-tier-only results still cache as before.
        if context and cache_key and not coverage_evidence:
            # User-scoped: shorter TTL + minimum result gate (avoid caching sparse results)
            _write_ttl = 900 if user_ctx else _CACHE_TTL
            if not user_ctx or len(context) >= 3:
                _redis.setex(cache_key, _write_ttl, json.dumps(context))

        if return_confidence:
            return context, round(_confidence, 2)
        return context

    except Exception as e:
        logger.error(f"Hybrid retrieval failed: {e}")
        return []


# ── Phase 3+4: LLMLingua-2 selective compression ─────────────────────────────

def _lingua_compress_if_enabled(chunks: list, question: str, namespace: str) -> list:
    """
    Apply LLMLingua-2 compression ONLY when:
      1. ENABLE_LINGUA_COMPRESS=true in env
      2. Namespace is in LINGUA_COMPRESS_NAMESPACES (prose only, never code repos)
      3. compression_svc at :8005 is reachable

    Phase 4: LINGUA_COMPRESS_SHADOW=true → compress in background, log diff,
    but serve ORIGINAL chunks (safe A/B rollout with zero user impact).
    """
    import os as _os
    if not _os.getenv("ENABLE_LINGUA_COMPRESS", "").lower() in ("true", "1", "yes"):
        return chunks
    if not chunks:
        return chunks

    # Phase 4: namespace gate — only prose namespaces, never code repos
    _allowed_ns = [
        ns.strip()
        for ns in _os.getenv(
            "LINGUA_COMPRESS_NAMESPACES",
            "docs_kb:confluence,docs_kb:platform"
        ).split(",")
        if ns.strip()
    ]
    _ns = namespace or ""
    # repo_* namespaces are always excluded (code content — risk of critical token loss)
    if _ns.startswith("repo_") or not any(_ns.startswith(ns) for ns in _allowed_ns):
        return chunks

    _shadow = _os.getenv("LINGUA_COMPRESS_SHADOW", "").lower() in ("true", "1", "yes")
    _ratio  = float(_os.getenv("LINGUA_COMPRESS_RATIO", "0.5"))
    # No hardcoded localhost default — unset fails the SEC-11 prefix check
    # below (empty string matches no allowed prefix), which already skips
    # compression and serves the uncompressed chunks.
    _svc    = _os.getenv("COMPRESS_SVC_URL", "")

    # SEC-11: validate COMPRESS_SVC_URL against approved internal prefixes
    _COMPRESS_ALLOWED_PREFIXES = [
        p.strip()
        for p in _os.getenv(
            "COMPRESS_SVC_ALLOWED_PREFIXES",
            "http://localhost:,http://127.0.0.1:,http://compress-svc"
        ).split(",")
        if p.strip()
    ]
    if not any(_svc.startswith(pfx) for pfx in _COMPRESS_ALLOWED_PREFIXES):
        logger.warning(
            f"[LINGUA] COMPRESS_SVC_URL {_svc!r} does not match approved prefixes "
            f"{_COMPRESS_ALLOWED_PREFIXES} — skipping compression (SEC-11)"
        )
        return chunks

    try:
        import httpx as _httpx
        resp = _httpx.post(
            f"{_svc}/compress",
            json={"chunks": chunks, "question": question, "ratio": _ratio},
            timeout=8.0,
        )
        if resp.status_code == 200:
            compressed = resp.json().get("chunks", chunks)
            _before = sum(len(c) for c in chunks)
            _after  = sum(len(c) for c in compressed)
            logger.info(
                f"[LINGUA] namespace={_ns!r} chunks={len(chunks)} "
                f"{_before:,}→{_after:,} chars "
                f"({100*(1-_after/max(_before,1)):.0f}% reduction)"
                + (" [SHADOW — serving original]" if _shadow else "")
            )
            try:
                from core.compress_metrics import record as _cmr
                _cmr("lingua_rag", _before, _after)
            except Exception:
                pass
            # Phase 4 shadow mode: log diff but serve original for safe A/B
            if _shadow:
                return chunks
            return compressed
        else:
            logger.warning(f"[LINGUA] compression_svc returned {resp.status_code} — serving original")
    except Exception as _e:
        logger.debug(f"[LINGUA] compression_svc unreachable ({_e}) — serving original")

    return chunks
