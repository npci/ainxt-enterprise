# SPDX-License-Identifier: MIT
# ============================================================
# IMPORTS
# ============================================================

from core.logger import logger
from core.prompts import (
    INTENT_CLASSIFIER_PROMPT,
    GENERAL_PROMPT,
    CODE_PROMPT
)

import json
import hashlib

from pydantic import PrivateAttr
from core.config import redis_client as _make_redis

from llama_index.core.schema import QueryBundle
try:
    from llama_index.embeddings.ollama import OllamaEmbedding
    from llama_index.core.embeddings import BaseEmbedding
    _LLAMA_EMBED_AVAILABLE = True
except ImportError:
    _LLAMA_EMBED_AVAILABLE = False


# ============================================================
# REDIS INIT
# ============================================================

redis_client = _make_redis(db=0, decode_responses=True)

RETRIEVAL_CACHE_TTL = 86400
EMBED_CACHE_TTL = 86400 * 7


# ============================================================
# CHROMA CLIENT (GLOBAL)
# ============================================================



# ============================================================
# LLM — in-house proxy via gateway_local_llm
# Ollama LLM inference is NOT used in prod.
# All LLM calls go through gateway_local_llm (LOCAL_LLM_BASE_URL).
# ============================================================

def _local_llm_generate(prompt: str, max_tokens: int = 200) -> str:
    """Single non-streaming call via in-house LLM proxy."""
    try:
        from gateway_local_llm import get_local_gateway
        return "".join(get_local_gateway().generate(prompt, tier="simple"))
    except Exception as e:
        logger.error(f"_local_llm_generate failed: {e}")
        return ""

# ============================================================
# RERANKER — pure Python RRF, no ML model, no downloads
# ============================================================

# ============================================================
# EMBEDDING MODEL WITH REDIS CACHE
# ============================================================

class CachedOllamaEmbedding(_LLAMA_EMBED_AVAILABLE and BaseEmbedding or object):
    """
    Legacy embedding wrapper — only instantiated if llama_index is available.
    In prod, all embeddings go through services/embed_svc (port 8001).
    This class is kept as a stub for backward compat only.
    """

    _model = None

    def __init__(self):
        if _LLAMA_EMBED_AVAILABLE:
            super().__init__()
            import os as _os
            self._model = OllamaEmbedding(model_name=_os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"))


    def _cache_key(self, prefix, text):

        key = hashlib.sha256(
            f"{prefix}:{text.strip().lower()}".encode()
        ).hexdigest()

        return f"embed:{key}"


    def _get_query_embedding(self, query):

        cache_key = self._cache_key("query", query)

        cached = redis_client.get(cache_key)

        if cached:
            return json.loads(cached)

        embedding = self._model.get_query_embedding(query)

        redis_client.setex(
            cache_key,
            EMBED_CACHE_TTL,
            json.dumps(embedding)
        )

        return embedding


    def _get_text_embedding(self, text):

        cache_key = self._cache_key("text", text)

        cached = redis_client.get(cache_key)

        if cached:
            return json.loads(cached)

        embedding = self._model.get_text_embedding(text)

        redis_client.setex(
            cache_key,
            EMBED_CACHE_TTL,
            json.dumps(embedding)
        )

        return embedding


    async def _aget_query_embedding(self, query):
        return self._get_query_embedding(query)


    async def _aget_text_embedding(self, text):
        return self._get_text_embedding(text)


embed_model = CachedOllamaEmbedding() if _LLAMA_EMBED_AVAILABLE else None


# ============================================================
# RETRIEVER — always built fresh so similarity_top_k changes
# take effect immediately without a server restart.
# VectorStoreIndex.from_vector_store() wraps an existing
# ChromaDB collection — no document ingestion, < 50 ms.
# ============================================================

_SIMILARITY_TOP_K = 15


def get_retriever(repo_filter=None):
    """
    ChromaDB removed — retrieval now goes through pgvector (hybrid_search.py).
    Returns None so callers fall through to the pgvector path.
    """
    return None


# ============================================================
# HYBRID RETRIEVAL ENGINE
# ============================================================

def semantic_search(repo_filter, question, retriever_getter):

    try:

        retriever = retriever_getter(repo_filter)

        if not retriever:
            return []

        nodes = retriever.retrieve(QueryBundle(query_str=question))

        results = []

        for n in nodes:
            results.append({
                "text": n.node.text,
                "score": float(n.score or 0.0),
                "metadata": n.node.metadata
            })

        return results

    except Exception as e:
        logger.error(f"Semantic search failed: {e}")
        return []



def keyword_search(repo_filter, question, top_k=20):
    """ChromaDB removed — use models/hybrid_search.keyword_search() instead."""
    return []


def _keyword_search_legacy(repo_filter, question, top_k=20):
    """Legacy ChromaDB keyword search — kept for reference only, never called."""
    try:
        if not repo_filter:
            return []
        collection_name = f"repo_{repo_filter.lower()}"
        collection = None  # chroma_client removed

        # Use SAME embedding model used during indexing
        query_embedding = embed_model.get_query_embedding(question)

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "distances", "metadatas"]
        )

        docs = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0]

        output = []

        for i, doc in enumerate(docs):

            if not doc:
                continue

            # Convert cosine distance → similarity score
            score = 1.0 - float(distances[i]) if i < len(distances) else 0.5

            output.append({
                "text": doc.strip(),
                "score": score,
                "source": "keyword"
            })

        logger.info(f"Keyword results: {len(output)}")

        return output

    except Exception as e:

        logger.error(f"Keyword search failed: {e}")

        return []


def metadata_search(repo_filter, query, top_k=5):
    """ChromaDB removed — use models/hybrid_search.symbol_search() instead."""
    return []


def _metadata_search_legacy(repo_filter, query, top_k=5):
    """Legacy ChromaDB metadata search — kept for reference only, never called."""
    try:
        if not repo_filter:
            return []
        collection_name = f"repo_{repo_filter.lower()}"
        collection = None  # chroma_client removed

        keywords = query.split()

        output = []

        for word in keywords:

            if len(word) < 4:
                continue

            results = collection.get(
                where={"class": word},
                limit=top_k
            )

            docs = results.get("documents", [])

            for doc in docs:

                output.append({
                    "text": doc.strip(),
                    "score": 2.0,  # boost metadata matches
                    "source": "metadata"
                })

        return output

    except Exception as e:

        logger.error(f"Metadata search failed: {e}")

        return []


def merge_and_rerank(results, top_k=6):
    """
    Merge all search results and rerank
    """

    try:

        unique = {}

        for item in results:

            text = item["text"]

            score = item["score"]

            if text not in unique:

                unique[text] = score

            else:

                unique[text] = max(unique[text], score)

        merged = [

            {"text": k, "score": v}

            for k, v in unique.items()

        ]

        merged.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        return merged[:top_k]

    except Exception as e:

        logger.error(f"Merge failed: {e}")

        return []

def cross_encoder_rerank(query, candidates, top_k=5):
    """Rerank via embed svc HTTP (:8001/rerank) — BGE reranker runs in embed svc, never in uvicorn."""
    import httpx
    import os
    try:
        # No hardcoded localhost default — same env var as core.config.EMBED_SVC_URL;
        # an empty value here fails the request below, which is already caught and
        # falls back to the local RRF reranker.
        _svc_url = os.getenv("EMBED_SVC_URL", "")
        resp = httpx.post(
            f"{_svc_url}/rerank",
            json={"query": query, "candidates": candidates, "top_k": top_k},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["results"]
    except Exception as e:
        logger.warning(f"cross_encoder_rerank: embed svc unavailable ({e}), RRF fallback")
        from models.hybrid_search import merge_and_rerank
        return merge_and_rerank(candidates, top_k=top_k)

# ============================================================
# CACHE KEY
# ============================================================

def get_retrieval_cache_key(question, repo_filter):

    raw = f"{question.strip().lower()}:{repo_filter}"

    key = hashlib.sha256(raw.encode()).hexdigest()

    return f"retrieval:v1:{key}"


# ============================================================
# INTENT CLASSIFIER
# ============================================================

def classify_intent(question):
    try:
        prompt = INTENT_CLASSIFIER_PROMPT.format(question=question)
        raw = _local_llm_generate(prompt, max_tokens=5).strip().upper()
        return "code" if raw == "CODE" else "general"
    except Exception as e:
        logger.error(f"Intent classifier failed: {e}")
        return "general"


# ============================================================
# CONTEXT RETRIEVAL (MULTI-REPO)
# ============================================================
def hybrid_retrieve_context(question, repo_filter):
    """
    Enterprise-grade hybrid retrieval pipeline.

    Order of intelligence:

    metadata search      (highest precision)
    semantic search      (embedding similarity)
    keyword search       (exact match safety)
    cross-encoder rerank (final ranking)
    """

    try:

        # ============================================================
        # CACHE CHECK
        # ============================================================

        cache_key = get_retrieval_cache_key(
            question,
            repo_filter
        )

        cached = redis_client.get(cache_key)

        if cached:
            logger.info("Retrieval cache hit")
            return json.loads(cached)


        # ============================================================
        # STEP 1 — SEMANTIC SEARCH
        # ============================================================

        semantic = semantic_search(
            repo_filter,
            question,
            get_retriever
        ) or []


        # ============================================================
        # STEP 2 — KEYWORD SEARCH
        # ============================================================

        keyword = keyword_search(
            repo_filter,
            question
        ) or []


        # ============================================================
        # STEP 3 — METADATA SEARCH (CRITICAL)
        # ============================================================

        metadata = metadata_search(
            repo_filter,
            question
        ) or []


        logger.info(
            f"Retrieval candidates → semantic={len(semantic)} "
            f"keyword={len(keyword)} metadata={len(metadata)}"
        )


        # ============================================================
        # STEP 4 — MERGE CANDIDATES
        # ============================================================

        candidates = semantic + keyword + metadata

        # STEP 4b — ChromaDB docs_kb removed; KB search now in gateway fast-path
        # via pgvector (hybrid_search.pgvector_search on docs_kb:* namespaces).
        pass

        if not candidates:
            logger.warning("No retrieval candidates found")
            return []


        merged = merge_and_rerank(
            candidates,
            top_k=20
        )


        # ============================================================
        # STEP 5 — CROSS-ENCODER RERANK (FINAL INTELLIGENCE)
        # ============================================================

        reranked = cross_encoder_rerank(
            question,
            merged,
            top_k=10
        )


        # ============================================================
        # STEP 6 — FINAL CONTEXT BUILD
        # ============================================================

        context = []

        for item in reranked:

            text = item.get("text")

            if text:
                context.append(text[:1500])


        logger.info(f"Final context chunks: {len(context)}")


        # ============================================================
        # STEP 7 — CACHE STORE
        # ============================================================

        redis_client.setex(
            cache_key,
            RETRIEVAL_CACHE_TTL,
            json.dumps(context)
        )


        return context


    except Exception as e:

        logger.error(f"Hybrid retrieval failed: {e}")

        return []

# ============================================================
# MAIN QUERY FUNCTION
# ============================================================

def get_query_response(question, repo_filter=None):

    intent = classify_intent(question)

    context_used = False

    if intent == "general":

        prompt = GENERAL_PROMPT.format(
            question=question
        )

    else:

        context_list = hybrid_retrieve_context(
            question,
            repo_filter
        )

        context = "\n\n".join(context_list)

        if context:

            context_used = True

            prompt = CODE_PROMPT.format(
                context=context,
                question=question
            )

        else:

            prompt = GENERAL_PROMPT.format(
                question=question
            )


    try:
        from gateway_local_llm import get_local_gateway
        _gw = get_local_gateway()

        def generator():
            try:
                for tok in _gw.stream(prompt, tier="simple"):
                    if tok:
                        yield tok
            except Exception as e:
                logger.error(e)
                yield "\n[Generation interrupted]"

        return generator(), "local", 1.0 if context_used else 0.3

    except Exception as e:
        logger.error(e)
        return iter(["Generation failed"]), "local", 0


# ============================================================
# DRAFT ANSWER
# ============================================================

def generate_draft_answer(question):
    try:
        from gateway_local_llm import get_local_gateway
        gw = get_local_gateway()
        for tok in gw.stream(f"Answer briefly:\n{question}", tier="simple"):
            if tok:
                yield tok
    except Exception:
        yield ""


# ============================================================
# MODEL WARMUP
# ============================================================

def warm_load_model():
    logger.info("Warming up local LLM proxy")
    try:
        from gateway_local_llm import get_local_gateway
        gw = get_local_gateway()
        gw.generate("warmup", tier="simple")
        logger.info("Local LLM proxy warmup complete")
    except Exception as e:
        logger.warning(f"Local LLM warmup skipped (not configured or unavailable): {e}")



logger.info("Local model ready (multi-repo enabled)")

# ============================================================
# ENTERPRISE AGENT LLM WRAPPER (NON-DESTRUCTIVE ADDITION)
# ============================================================

class LocalLLM:
    """
    Agent framework LLM wrapper — routes through in-house LLM proxy
    (gateway_local_llm / LOCAL_LLM_BASE_URL). No Ollama LLM inference.
    """

    def __init__(self):
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

    def complete(self, prompt: str) -> str:
        try:
            from gateway_local_llm import get_local_gateway
            result = get_local_gateway().generate(prompt, tier="simple")
            self._last_output_tokens = max(1, len(result.split()))
            return result
        except Exception as e:
            logger.error(f"LocalLLM.complete failed: {e}")
            return ""

    def stream(self, prompt: str):
        self._last_input_tokens  = 0
        self._last_output_tokens = 0
        try:
            from gateway_local_llm import get_local_gateway
            for tok in get_local_gateway().stream(prompt, tier="simple"):
                if tok:
                    self._last_output_tokens += 1
                    yield tok
        except Exception as e:
            logger.error(f"LocalLLM.stream failed: {e}")
            yield ""


# Global instance used by agent framework
agent_llm = LocalLLM()