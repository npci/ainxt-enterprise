# SPDX-License-Identifier: MIT
"""
core/kb_retrieval.py
====================
Pure KB retrieval function — extracted from gateway.py's ask_ai() fast-path.

This module owns ONLY the retrieval pipeline:
  namespace discovery → pgvector + BM25 → threshold filter → dedup →
  BGE reranker → follow-up fallback retry → source metadata →
  disambiguation gate → coverage retriever → (_docs_context, _fp_sources_meta)

It has NO knowledge of:
  - HTTP request/response (no FastAPI imports)
  - LLM calls (no model_router)
  - Kafka / Redis persistence
  - ainxt-api / Rust runtime

Inputs are plain Python values passed as arguments.
Outputs are a plain dataclass so callers never need to import this module
just to type-check the result.

Why a separate module?
  gateway.py is edited by multiple teams simultaneously. Any change to the
  Chat path (ainxt-api routing, canary cohort, doc-context notifications)
  lands in the same file as the KB retrieval logic, causing unintended
  regressions. Isolating retrieval here means Chat PRs never touch this file.
"""

from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from core.logger import logger


# ---------------------------------------------------------------------------
# Trivial-query regex — same pattern as gateway.py module level.
# Defined here so kb_ask_router.py can import it without touching gateway.py.
# ---------------------------------------------------------------------------
import re as _re

TRIVIAL_QUERY_RE = _re.compile(
    r"^(hi+|hello+|hey+|thanks?|thank\s+you|bye+|good\s+(morning|afternoon|evening|night)|"
    r"what\s+is\s+\d+\s*[\+\-\*\/]\s*\d+|how\s+are\s+you|"
    r"who\s+are\s+you|what\s+(can|do)\s+you\s+do|okay|ok|sure|cool|got\s+it)\??\.?\s*$",
    _re.IGNORECASE,
)


class SkipKBProbe(Exception):
    """Internal sentinel — raised to short-circuit the probe when rag_mode='off'
    or the query is trivial. Caught by the outer try/except in run_kb_retrieval()."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class KBRetrievalResult:
    """Return value of run_kb_retrieval().

    docs_context    : str   — retrieved text to inject into the LLM prompt.
                              Empty string when nothing was found.
    sources_meta    : list  — list of source dicts for the citations panel
                              (title, snippet, doc_id, score, …).
    disambig_payload: dict | None
                            — when not None, the caller must return a
                              __clarify__ SSE frame to the client instead of
                              calling the LLM. Contains {message, candidates,
                              question, rag_mode}.
    """
    docs_context:     str        = ""
    sources_meta:     list       = field(default_factory=list)
    disambig_payload: Optional[dict] = None


# ---------------------------------------------------------------------------
# Main retrieval function
# ---------------------------------------------------------------------------

def run_kb_retrieval(
    *,
    # ── Query strings ──────────────────────────────────────────────────────
    safe_question:      str,          # PII-masked user question
    rag_query:          str,          # condensed standalone query for pgvector
    bm25_query:         str,          # query for BM25 keyword search
    # ── Flags ──────────────────────────────────────────────────────────────
    is_trivial_q:       bool,
    kb_probe_enabled:   bool,
    runtime_will_handle: bool,
    is_followup:        bool,
    has_history:        bool,
    # ── Scope / context ────────────────────────────────────────────────────
    user_ctx:           dict,         # enriched user context (scope_filter, kb_doc_id, …)
    chat_scope_doc_ids: list,         # user-selected doc IDs from DocPickerCard
    agent_kb_namespace: Optional[str],
    rag_mode:           str,
    request_id:         str,
    # ── Redis KV client (for namespace discovery) ──────────────────────────
    redis_ns_client,                  # get_kv(RDB_CACHE, decode_responses=True)
) -> KBRetrievalResult:
    """
    Run the full KB retrieval pipeline and return a KBRetrievalResult.

    The caller (kb_ask_router.py) inspects the result:
      • If disambig_payload is set → return a __clarify__ SSE response.
      • Otherwise → inject docs_context into the LLM prompt.

    This function never raises — all errors are caught and logged.
    On any failure it returns an empty KBRetrievalResult so the caller
    falls through to a plain LLM answer (graceful degradation).
    """
    result = KBRetrievalResult()
    _t_probe_start = time.time()

    do_kb_probe = (not is_trivial_q) and kb_probe_enabled and not runtime_will_handle

    try:
        if not do_kb_probe:
            raise SkipKBProbe()

        from models.hybrid_search import pgvector_search, keyword_search

        # ── Namespace discovery ────────────────────────────────────────────
        _t_ns = time.time()
        _kb_namespaces = redis_ns_client.smembers("docs:namespaces") or set()
        if not _kb_namespaces:
            try:
                from db.database import VectorSessionLocal as _VecSess
                from sqlalchemy import text as _nsql
                _vdb = _VecSess()
                try:
                    _ns_rows = _vdb.execute(_nsql(
                        "SELECT DISTINCT REPLACE(repo, 'docs_kb:', '') "
                        "FROM document_embeddings WHERE repo LIKE 'docs_kb:%'"
                    )).fetchall()
                    _kb_namespaces = {row[0] for row in _ns_rows}
                    for _rns in _kb_namespaces:
                        redis_ns_client.sadd("docs:namespaces", _rns)
                finally:
                    _vdb.close()
            except Exception:
                pass

        _all_kb_ns = set(_kb_namespaces)

        # ── Phase 1: restrict namespaces to scope ──────────────────────────
        _scope_for_probe  = (user_ctx or {}).get("scope_filter") or {}
        _kb_doc_for_probe = (user_ctx or {}).get("kb_doc_id")

        if _kb_doc_for_probe or _scope_for_probe.get("product_id") or _scope_for_probe.get("domain"):
            try:
                from db.database import SessionLocal as _NsSL
                from sqlalchemy import text as _nsql2
                _nsdb = _NsSL()
                try:
                    if _kb_doc_for_probe:
                        _ns_rows = _nsdb.execute(
                            _nsql2("SELECT DISTINCT namespace FROM knowledge_docs WHERE id::text = :did"),
                            {"did": str(_kb_doc_for_probe)},
                        ).fetchall()
                    elif _scope_for_probe.get("product_id"):
                        _params = {"pid": str(_scope_for_probe["product_id"])}
                        _where  = "product_id::text = :pid AND status = 'ACTIVE'"
                        if _scope_for_probe.get("domain"):
                            _where += " AND domain = :dom"
                            _params["dom"] = _scope_for_probe["domain"]
                        if _scope_for_probe.get("spec_version"):
                            _where += " AND spec_version = :ver"
                            _params["ver"] = _scope_for_probe["spec_version"]
                        _ns_rows = _nsdb.execute(
                            _nsql2(f"SELECT DISTINCT namespace FROM knowledge_docs WHERE {_where}"),
                            _params,
                        ).fetchall()
                    else:
                        _params = {"dom": _scope_for_probe["domain"]}
                        _where  = "domain = :dom AND status = 'ACTIVE'"
                        if _scope_for_probe.get("spec_version"):
                            _where += " AND spec_version = :ver"
                            _params["ver"] = _scope_for_probe["spec_version"]
                        _ns_rows = _nsdb.execute(
                            _nsql2(f"SELECT DISTINCT namespace FROM knowledge_docs WHERE {_where}"),
                            _params,
                        ).fetchall()
                    _scoped_ns = {r[0] for r in _ns_rows if r and r[0]}
                    if _scoped_ns:
                        _all_kb_ns = _scoped_ns
                        logger.debug(
                            f"[kb_retrieval] scope-restricted namespaces → "
                            f"{len(_all_kb_ns)} (scope_filter={_scope_for_probe} "
                            f"kb_doc_id={_kb_doc_for_probe})"
                        )
                    else:
                        _all_kb_ns = set()
                        logger.info(
                            f"[kb_retrieval] scope had no matching docs → "
                            f"skipping namespace probe (scope_filter={_scope_for_probe} "
                            f"kb_doc_id={_kb_doc_for_probe})"
                        )
                finally:
                    _nsdb.close()
            except Exception as _ns_err:
                logger.warning(
                    f"[kb_retrieval] scope namespace lookup failed ({_ns_err}) — "
                    f"falling back to full namespace iteration."
                )

        logger.info(
            f"[kb_retrieval] namespace discovery done | "
            f"namespaces={len(_all_kb_ns)} elapsed={time.time() - _t_ns:.3f}s"
        )

        # ── pgvector + BM25 search ─────────────────────────────────────────
        _t_search = time.time()
        _raw_results: list = []
        for _ns in _all_kb_ns:
            _repo = f"docs_kb:{_ns.lower()}"
            _raw_results += pgvector_search(_repo, rag_query, top_k=10, user_ctx=user_ctx or None)
            _raw_results += keyword_search(_repo, bm25_query, user_ctx=user_ctx or None)

        # Agent-scoped KB search
        if agent_kb_namespace:
            try:
                _agent_kb_results = pgvector_search(
                    agent_kb_namespace, rag_query, top_k=6, user_ctx=None
                )
                _agent_kb_results += keyword_search(
                    agent_kb_namespace, bm25_query, user_ctx=None
                )
                for _akr in _agent_kb_results:
                    _akr["score"] = _akr.get("score", 0) * 1.5
                _raw_results = _agent_kb_results + _raw_results
                logger.info(f"[kb_retrieval] Agent KB search: {agent_kb_namespace} → {len(_agent_kb_results)} results")
            except Exception as _akb_err:
                logger.warning(f"[kb_retrieval] Agent KB search failed: {_akb_err}")

        logger.info(
            f"[kb_retrieval] search done | raw={len(_raw_results)} "
            f"elapsed={time.time() - _t_search:.3f}s"
        )

        # ── Threshold filter ───────────────────────────────────────────────
        _filtered: list = []
        for _res in _raw_results:
            _src = _res.get("source", "pgvector")
            _sc  = _res.get("score", 0)
            _threshold = 0.35 if _src == "pgvector" else 0.005
            if _res.get("text", "").strip() and _sc > _threshold:
                _filtered.append(_res)

        # ── Deduplication by text prefix ───────────────────────────────────
        _seen_prefixes: set = set()
        _deduped: list = []
        for _res in sorted(_filtered, key=lambda x: x.get("score", 0), reverse=True):
            _prefix = (_res.get("text") or "")[:200]
            if _prefix and _prefix not in _seen_prefixes:
                _seen_prefixes.add(_prefix)
                _deduped.append(_res)

        # ── BGE Reranker ───────────────────────────────────────────────────
        _rr_passed_any = True
        _t_reranker = time.time()
        if _deduped:
            try:
                from models.hybrid_retriever import _rerank_via_svc as _fp_rerank
                _reranked = _fp_rerank(
                    safe_question,
                    _deduped,
                    top_k=min(len(_deduped), 20),
                )
                if _reranked:
                    _rr_min = float(os.getenv("RERANKER_MIN_SCORE", "0.30"))
                    _rr_passed = [r for r in _reranked if r.get("score", 0) >= _rr_min]
                    _rr_passed_any = bool(_rr_passed)
                    _deduped = _rr_passed if _rr_passed else _reranked[:3]
                    logger.info(
                        f"[kb_retrieval] reranked {len(_reranked)} candidates → "
                        f"{len(_deduped)} passed threshold={_rr_min} "
                        f"top_score={_deduped[0].get('score', 0):.3f} "
                        f"elapsed={time.time() - _t_reranker:.3f}s"
                    )
                else:
                    logger.warning("[kb_retrieval] reranker returned empty — keeping _deduped as-is")
            except Exception as _rr_err:
                logger.warning(f"[kb_retrieval] reranker failed ({_rr_err}) — using raw deduped order")

        # ── Follow-up fallback retry ───────────────────────────────────────
        if is_followup and not _rr_passed_any and _all_kb_ns:
            try:
                logger.info(
                    "[kb_retrieval] condensed follow-up query yielded low-confidence "
                    "results — retrying retrieval with bare question"
                )
                _fallback_raw: list = []
                for _ns_fb in _all_kb_ns:
                    _repo_fb = f"docs_kb:{_ns_fb.lower()}"
                    _fallback_raw += pgvector_search(_repo_fb, safe_question, top_k=10, user_ctx=user_ctx or None)
                    _fallback_raw += keyword_search(_repo_fb, safe_question, user_ctx=user_ctx or None)

                _fallback_filtered = [
                    r for r in _fallback_raw
                    if r.get("text", "").strip()
                    and r.get("score", 0) > (0.35 if r.get("source", "pgvector") == "pgvector" else 0.005)
                ]
                _fb_seen: set = set()
                _fallback_deduped: list = []
                for _res_fb in sorted(_fallback_filtered, key=lambda x: x.get("score", 0), reverse=True):
                    _prefix_fb = (_res_fb.get("text") or "")[:200]
                    if _prefix_fb and _prefix_fb not in _fb_seen:
                        _fb_seen.add(_prefix_fb)
                        _fallback_deduped.append(_res_fb)

                if _fallback_deduped:
                    from models.hybrid_retriever import _rerank_via_svc as _fp_rerank_fb
                    _fallback_reranked = _fp_rerank_fb(
                        safe_question, _fallback_deduped,
                        top_k=min(len(_fallback_deduped), 20),
                    )
                    if _fallback_reranked:
                        _fb_rr_min = float(os.getenv("RERANKER_MIN_SCORE", "0.30"))
                        _fb_passed = [r for r in _fallback_reranked if r.get("score", 0) >= _fb_rr_min]
                        _fallback_candidates = _fb_passed if _fb_passed else _fallback_reranked[:3]
                        _fallback_top = _fallback_candidates[0].get("score", 0) if _fallback_candidates else 0.0
                        _condensed_top = _deduped[0].get("score", 0) if _deduped else 0.0
                        if _fallback_top > _condensed_top:
                            logger.info(
                                f"[kb_retrieval] fallback (bare question) scored higher "
                                f"({_fallback_top:.3f} > {_condensed_top:.3f}) — using fallback results"
                            )
                            _deduped = _fallback_candidates
            except Exception as _fb_err:
                logger.warning(f"[kb_retrieval] follow-up fallback retry failed (non-fatal): {_fb_err}")

        # ── Source metadata ────────────────────────────────────────────────
        _docs_chunks = [r["text"] for r in _deduped]
        _top_score   = _filtered[0].get("score", 0) if _filtered else 0.0

        for _src in _deduped[:6]:
            _doc_id_src = _src.get("doc_id") or ""
            result.sources_meta.append({
                "title":        (_src.get("doc_name") or _src.get("title")
                                 or _src.get("file_path") or "")[:120],
                "snippet":      (_src.get("text") or "")[:300],
                "file_path":    _src.get("file_path") or "",
                "namespace":    _src.get("repo") or "",
                "score":        float(_src.get("score", 0) or 0),
                "doc_id":       _doc_id_src,
                "doc_name":     _src.get("doc_name") or "",
                "section_name": _src.get("section_name") or "",
                "section_path": _src.get("section_path") or "",
                "page_number":  _src.get("page_number"),
                "source_type":  _src.get("source_type") or "",
                "original_url": f"/api/kb/original/{_doc_id_src}" if _doc_id_src else "",
            })

        if _docs_chunks:
            result.docs_context = "\n\n".join(_docs_chunks[:6])
            logger.info(
                f"[kb_retrieval] hit — {len(_docs_chunks)} chunks "
                f"(namespaces: {sorted(_all_kb_ns)}, top_score={_top_score:.3f})"
            )
        else:
            _raw_top = _raw_results[0].get("score", 0) if _raw_results else 0.0
            logger.info(
                f"[kb_retrieval] miss — {len(_raw_results)} raw results, "
                f"top_score={_raw_top:.3f} (below threshold, namespaces={sorted(_all_kb_ns)})"
            )

        # ── Disambiguation gate ────────────────────────────────────────────
        _fp_doc_id_for_disambig = (user_ctx or {}).get("kb_doc_id")
        if _deduped and not chat_scope_doc_ids and not _fp_doc_id_for_disambig and not has_history:
            try:
                _fp_scope_for_disambig = (user_ctx or {}).get("scope_filter") or {}
                _at_scope_level = bool(
                    _fp_scope_for_disambig.get("domain")
                    or _fp_scope_for_disambig.get("product_id")
                )
                _DISAMBIG_MIN = 2 if _at_scope_level else int(os.getenv("KB_DISAMBIG_MIN_DOCS", "4"))

                _fp_disambig_chunks: dict = {}
                _fp_disambig_meta: dict   = {}
                for _dr in _deduped:
                    _ddid = str(_dr.get("doc_id") or "")
                    if not _ddid:
                        continue
                    if _ddid not in _fp_disambig_meta:
                        _fp_disambig_meta[_ddid] = {
                            "doc_id":   _ddid,
                            "doc_name": (_dr.get("doc_name") or _dr.get("file_path") or _ddid)[:120],
                        }
                    _fp_disambig_chunks.setdefault(_ddid, []).append(float(_dr.get("score", 0)))

                def _top2_avg(_scores: list) -> float:
                    _top2 = sorted(_scores, reverse=True)[:2]
                    return sum(_top2) / len(_top2) if _top2 else 0.0

                _fp_disambig_list = sorted(
                    _fp_disambig_meta.values(),
                    key=lambda x: _top2_avg(_fp_disambig_chunks.get(x["doc_id"], [])),
                    reverse=True,
                )
                _fp_disambig_count = len(_fp_disambig_list)

                if _fp_disambig_count >= _DISAMBIG_MIN:
                    _disambig_names_str = ", ".join(
                        f'"{c["doc_name"]}"' for c in _fp_disambig_list
                    )
                    if _at_scope_level:
                        _disambig_msg = (
                            f"I found {_fp_disambig_count} relevant document"
                            f"{'s' if _fp_disambig_count > 1 else ''} for your query. "
                            f"Select which one(s) to search in:"
                        )
                    else:
                        _disambig_msg = (
                            f"I found {_fp_disambig_count} related documents for your query: "
                            f"{_disambig_names_str}. "
                            f"Which document(s) would you like me to refer to?"
                        )
                    logger.info(
                        f"[kb_retrieval] disambiguation triggered — "
                        f"distinct_docs={_fp_disambig_count} (min={_DISAMBIG_MIN}) "
                        f"at_scope_level={_at_scope_level} "
                        f"docs={[c['doc_name'] for c in _fp_disambig_list]}"
                    )
                    result.disambig_payload = {
                        "message":    _disambig_msg,
                        "candidates": _fp_disambig_list,
                        "question":   safe_question,
                        "rag_mode":   rag_mode,
                    }
                    return result
                else:
                    logger.info(
                        f"[kb_retrieval] disambiguation skipped — "
                        f"distinct_docs={_fp_disambig_count} (below min={_DISAMBIG_MIN}) → answering directly"
                    )
            except Exception as _disambig_err:
                logger.warning(f"[kb_retrieval] disambiguation gate error ({_disambig_err}) — answering directly")

        # ── Coverage retriever ─────────────────────────────────────────────
        try:
            from core.config import KB_RETRIEVAL_SCOPE as _FP_RS
            _fp_doc_id      = (user_ctx or {}).get("kb_doc_id")
            _fp_scope       = (user_ctx or {}).get("scope_filter") or {}
            _fp_cov_enabled = os.getenv("KB_COVERAGE_ENABLED", "true").lower() in ("1", "true", "yes")
            _FP_RS_effective = "full_file" if _fp_doc_id else _FP_RS

            if (
                _FP_RS_effective in ("both", "full_file")
                and _fp_cov_enabled
                and (_fp_doc_id or _fp_scope.get("product_id") or _fp_scope.get("domain") or bool(chat_scope_doc_ids))
            ):
                from store import kb_doc_cache as _fp_kb_cache
                from models.coverage_retriever import run_coverage as _fp_run_coverage
                _t_coverage = time.time()
                logger.info(
                    f"[kb_retrieval] coverage entering — "
                    f"effective_mode={_FP_RS_effective} configured_mode={_FP_RS} "
                    f"kb_doc_id={_fp_doc_id} scope={_fp_scope}"
                )

                _fp_best_doc_id = None

                if chat_scope_doc_ids:
                    # DocPickerCard re-query path
                    _fp_top_doc_ids = chat_scope_doc_ids
                    _fp_best_doc_rs = "full_file"
                    logger.info(
                        f"[kb_retrieval] DocPickerCard re-query — "
                        f"user-selected docs={_fp_top_doc_ids}"
                    )
                    _fp_docs_to_run = _fp_top_doc_ids
                    _all_cov_blocks = []
                    for _fp_each_doc in _fp_docs_to_run:
                        if not _fp_each_doc:
                            continue
                        _fp_payload = _fp_kb_cache.get_or_warm(
                            _fp_scope.get("product_id"),
                            _fp_scope.get("spec_version"),
                            _fp_each_doc,
                        )
                        if not _fp_payload:
                            logger.warning(
                                f"[kb_retrieval] cache miss doc_id={_fp_each_doc} — attempting force re-warm"
                            )
                            try:
                                _fp_payload = _fp_kb_cache.warm(_fp_each_doc)
                            except Exception as _rewarm_err:
                                logger.error(
                                    f"[kb_retrieval] re-warm failed doc_id={_fp_each_doc} error='{_rewarm_err}'"
                                )
                        if not _fp_payload:
                            logger.error(
                                f"[kb_retrieval] skipping doc_id={_fp_each_doc} — payload unavailable after re-warm"
                            )
                            continue
                        _fp_cov = _fp_run_coverage(
                            rag_query, _fp_payload, _deduped,
                            retrieval_scope=_fp_best_doc_rs,
                        )
                        _fp_cov_texts = [
                            e.get("text", "") for e in (_fp_cov.evidence or [])
                            if e.get("text")
                        ]
                        logger.info(
                            f"[kb_retrieval] DocPickerCard doc={_fp_each_doc} "
                            f"cov_sections={_fp_cov.sections_included}/{_fp_cov.sections_examined} "
                            f"cov_texts={len(_fp_cov_texts)}"
                        )
                        if _fp_cov_texts:
                            _fp_doc_label = (
                                _fp_payload.get("doc_name")
                                or _fp_payload.get("name")
                                or _fp_each_doc
                            )
                            _fp_labeled_block = (
                                f"=== Document: {_fp_doc_label} ===\n\n"
                                + "\n\n".join(_fp_cov_texts)
                            )
                            _all_cov_blocks.append(_fp_labeled_block)

                    if _all_cov_blocks:
                        _docs_chunks  = _all_cov_blocks
                        result.docs_context = "\n\n---\n\n".join(_all_cov_blocks)
                        logger.info(
                            f"[kb_retrieval] DocPickerCard merged — "
                            f"total_doc_blocks={len(_all_cov_blocks)}"
                        )
                        _selected_doc_id_set = set(chat_scope_doc_ids)
                        result.sources_meta = [
                            s for s in result.sources_meta
                            if s.get("doc_id") in _selected_doc_id_set
                        ]
                        logger.info(
                            f"[kb_retrieval] DocPickerCard sources filtered — "
                            f"selected_docs={chat_scope_doc_ids} "
                            f"sources_after_filter={len(result.sources_meta)}"
                        )

                elif _fp_doc_id:
                    # Single-document path
                    _fp_best_doc_id = _fp_doc_id
                    _fp_best_doc_rs = _FP_RS_effective  # "full_file"

                else:
                    # Product-level path — pick top-3 docs by reranker score
                    _fp_doc_chunk_scores: dict = {}
                    for _fp_hit in _deduped:
                        _fp_hit_doc = _fp_hit.get("doc_id") or ""
                        if _fp_hit_doc:
                            _fp_doc_chunk_scores.setdefault(str(_fp_hit_doc), []).append(
                                float(_fp_hit.get("score", 0))
                            )

                    def _fp_top2_avg(_scores: list) -> float:
                        _top2 = sorted(_scores, reverse=True)[:2]
                        return sum(_top2) / len(_top2) if _top2 else 0.0

                    _fp_doc_scores: dict = {
                        _did: _fp_top2_avg(_scs) for _did, _scs in _fp_doc_chunk_scores.items()
                    }

                    _fp_top_doc_ids: list = []
                    if _fp_doc_scores:
                        _fp_top_doc_ids = sorted(
                            _fp_doc_scores,
                            key=_fp_doc_scores.get,
                            reverse=True,
                        )[:3]
                        logger.info(
                            f"[kb_retrieval] product-level reranker-score doc selection — "
                            f"doc_scores={_fp_doc_scores} → top3={_fp_top_doc_ids}"
                        )
                    else:
                        logger.warning(
                            f"[kb_retrieval] product-level doc selection — "
                            f"all {len(_deduped)} chunks have doc_id=None (legacy chunks). "
                            f"Falling back to DB query."
                        )

                    if not _fp_top_doc_ids:
                        try:
                            from db.database import SessionLocal as _FpDocSL
                            from sqlalchemy import text as _fpdoc_sql
                            _fpdoc_db = _FpDocSL()
                            try:
                                _fpdoc_params = {}
                                if _fp_scope.get("product_id"):
                                    _fpdoc_params["pid"] = str(_fp_scope["product_id"])
                                    _fpdoc_where = "product_id::text = :pid AND status = 'ACTIVE'"
                                    if _fp_scope.get("domain"):
                                        _fpdoc_where += " AND domain = :dom"
                                        _fpdoc_params["dom"] = _fp_scope["domain"]
                                elif _fp_scope.get("domain"):
                                    _fpdoc_params["dom"] = _fp_scope["domain"]
                                    _fpdoc_where = "domain = :dom AND status = 'ACTIVE'"
                                else:
                                    _fpdoc_where = "status = 'ACTIVE'"
                                if _fp_scope.get("spec_version"):
                                    _fpdoc_where += " AND spec_version = :ver"
                                    _fpdoc_params["ver"] = _fp_scope["spec_version"]
                                _fpdoc_rows = _fpdoc_db.execute(
                                    _fpdoc_sql(
                                        f"SELECT id FROM knowledge_docs "
                                        f"WHERE {_fpdoc_where} "
                                        f"ORDER BY created_at DESC LIMIT 3"
                                    ),
                                    _fpdoc_params,
                                ).fetchall()
                                _fp_top_doc_ids = [str(r[0]) for r in _fpdoc_rows if r and r[0]]
                            finally:
                                _fpdoc_db.close()
                            logger.info(
                                f"[kb_retrieval] scope-level DB fallback — "
                                f"using {len(_fp_top_doc_ids)} most recent docs={_fp_top_doc_ids}"
                            )
                        except Exception as _fpdoc_err:
                            logger.warning(
                                f"[kb_retrieval] scope DB fallback failed ({_fpdoc_err}) — skipping coverage."
                            )

                    _fp_best_doc_rs = "full_file"

                # Run coverage on single-doc or product-level top-3
                if not chat_scope_doc_ids:
                    _all_cov_texts = []
                    _fp_docs_to_run = [_fp_best_doc_id] if _fp_doc_id else _fp_top_doc_ids

                    for _fp_each_doc in _fp_docs_to_run:
                        if not _fp_each_doc:
                            continue
                        _fp_payload = _fp_kb_cache.get_or_warm(
                            _fp_scope.get("product_id"),
                            _fp_scope.get("spec_version"),
                            _fp_each_doc,
                        )
                        if not _fp_payload:
                            logger.info(
                                f"[kb_retrieval] skipped — kb_doc_cache miss "
                                f"doc_id={_fp_each_doc} product_id={_fp_scope.get('product_id')} "
                                f"spec_version={_fp_scope.get('spec_version')}"
                            )
                            continue
                        _fp_cov = _fp_run_coverage(
                            rag_query, _fp_payload, _deduped,
                            retrieval_scope=_fp_best_doc_rs,
                        )
                        _fp_cov_texts = [
                            e.get("text", "") for e in (_fp_cov.evidence or [])
                            if e.get("text")
                        ]
                        _all_cov_texts.extend(_fp_cov_texts)
                        logger.info(
                            f"[kb_retrieval] doc={_fp_each_doc} "
                            f"effective_mode={_fp_best_doc_rs} configured_mode={_FP_RS} "
                            f"cov_sections={_fp_cov.sections_included}/{_fp_cov.sections_examined} "
                            f"badge='{_fp_cov.badge}' cov_texts={len(_fp_cov_texts)}"
                        )

                    if _all_cov_texts:
                        result.docs_context = "\n\n".join(_all_cov_texts)
                        logger.info(
                            f"[kb_retrieval] coverage merged — "
                            f"effective_mode={_fp_best_doc_rs} configured_mode={_FP_RS} "
                            f"total_cov_chunks={len(_all_cov_texts)} "
                            f"elapsed={time.time() - _t_coverage:.3f}s"
                        )

            elif _FP_RS_effective in ("both", "full_file") and not _fp_doc_id \
                    and not _fp_scope.get("product_id") and not _fp_scope.get("domain"):
                logger.info(
                    f"[kb_retrieval] coverage skipped — no kb_doc_id, no product_id, no domain "
                    f"(configured_mode={_FP_RS}). Returning RAG hits only."
                )
            elif _FP_RS_effective in ("both", "full_file") and not _fp_cov_enabled:
                logger.info(
                    f"[kb_retrieval] coverage disabled by KB_COVERAGE_ENABLED=false "
                    f"(effective_mode={_FP_RS_effective}). Returning RAG hits only."
                )

        except Exception as _fp_cov_err:
            logger.warning(f"[kb_retrieval] coverage dispatch failed (non-fatal): {_fp_cov_err}")

    except SkipKBProbe:
        # Generic mode (rag_mode="off") or trivial query — bypass KB probe
        pass
    except Exception as _kb_err:
        logger.warning(f"[kb_retrieval] probe failed (non-fatal): {_kb_err}")

    logger.info(
        f"[kb_retrieval] pipeline complete | "
        f"total_elapsed={time.time() - _t_probe_start:.3f}s "
        f"kb_hit={bool(result.docs_context)} sources={len(result.sources_meta)}"
    )
    return result
