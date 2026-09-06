# SPDX-License-Identifier: MIT
# ============================================================
# SEMANTIC CACHE & SEMANTIC MEMORY STORE
#
# L2 — Semantic Answer Cache
#   Table: ainxt.semantic_answer_cache
#   Threshold: 0.92 | TTL: SEMANTIC_CACHE_MAX_DAYS (default 7d)
#
# L3 — Semantic Memory
#   Table: ainxt.semantic_memory
#   Threshold: 0.75 | TTL: SEMANTIC_MEMORY_MAX_DAYS (default 90d)
#   Scope: user | team | org
#   Dedup:  exact (summary_hash) + semantic (embedding similarity ≥ 0.93)
#   Ranking: similarity×0.40 + confidence×0.25 + hit_count×0.15 + recency×0.20
#   Confidence cap: 0.95 — repetition reinforces but never reaches 1.0
#   Contradiction: flagged in prompt when ≥2 memories share topic
# ============================================================

import os
import hashlib
import json
from typing import Optional

import httpx
from sqlalchemy import text as _text

from db.database import vector_engine
from core.logger import logger

# ── Config ────────────────────────────────────────────────────────────────────

SEMANTIC_CACHE_ENABLED  = os.getenv("SEMANTIC_CACHE_ENABLED",  "true").lower() != "false"
SEMANTIC_MEMORY_ENABLED = os.getenv("SEMANTIC_MEMORY_ENABLED", "true").lower() != "false"

SEMANTIC_CACHE_THRESHOLD  = float(os.getenv("SEMANTIC_CACHE_THRESHOLD",  "0.92"))
SEMANTIC_MEMORY_THRESHOLD = float(os.getenv("SEMANTIC_MEMORY_THRESHOLD", "0.75"))
SEMANTIC_CACHE_MAX_DAYS   = int(os.getenv("SEMANTIC_CACHE_MAX_DAYS",   "7"))
SEMANTIC_MEMORY_MAX_DAYS  = int(os.getenv("SEMANTIC_MEMORY_MAX_DAYS",  "90"))
SEMANTIC_CACHE_MAX_RESULTS  = int(os.getenv("SEMANTIC_CACHE_MAX_RESULTS",  "3"))
SEMANTIC_MEMORY_MAX_RESULTS = int(os.getenv("SEMANTIC_MEMORY_MAX_RESULTS", "5"))
SEMANTIC_MEMORY_MIN_CONFIDENCE = float(os.getenv("SEMANTIC_MEMORY_MIN_CONFIDENCE", "0.70"))

# Semantic dedup: skip insert if a near-identical embedding already exists.
# 0.88 = near-synonym threshold measured against nomic-embed-text 768-dim.
# Lower than L2 cache (0.92) because we want to catch rephrased near-copies,
# but higher than L3 retrieval (0.75) so distinct-but-related memories survive.
SEMANTIC_DEDUP_THRESHOLD = float(os.getenv("SEMANTIC_DEDUP_THRESHOLD", "0.88"))

# Confidence cap: reinforcement can never push confidence above this
CONFIDENCE_MAX = float(os.getenv("SEMANTIC_CONFIDENCE_MAX", "0.95"))

# SEC-F-013 / ARCH-F-MISC-009: do not hardcode any environment-specific embed
# service URL. EMBED_SVC_URL must be set explicitly in every environment.
EMBED_SVC_URL = os.getenv("EMBED_SVC_URL", "").strip()
if not EMBED_SVC_URL:
    raise RuntimeError("EMBED_SVC_URL must be set.")

_embed_http = httpx.Client(
    base_url=EMBED_SVC_URL,
    timeout=10.0,
    limits=httpx.Limits(max_connections=5, max_keepalive_connections=5),
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _embed(text: str) -> Optional[list]:
    """Embed a single text via embed svc. Returns None on failure."""
    try:
        resp = _embed_http.post("/embed", json={"texts": [text], "provider": "ollama"})
        resp.raise_for_status()
        embs = resp.json().get("embeddings", [])
        return embs[0] if embs else None
    except Exception as e:
        logger.warning(f"[SemanticCache] embed svc unavailable: {e}")
        return None


def _vec_str(embedding: list) -> str:
    """Convert embedding list to pgvector literal '[v1,v2,...]'."""
    return "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"


def _summary_hash(memory_type: str, summary: str) -> str:
    """SHA-256 of 'type::summary[:1000]' — exact dedup key."""
    return hashlib.sha256(f"{memory_type}::{summary[:1000]}".encode()).hexdigest()


# Patterns whose answers MUST NOT be served from a shared cache
_IDENTITY_PATTERNS = [
    "who am i", "my name", "my role", "am i ", "my department",
    "my team", "my email", "my profile", "about me", "i am ",
]


def _is_identity_query(question: str) -> bool:
    """Return True if the question is about the current user's identity/profile."""
    q = question.lower()
    return any(p in q for p in _IDENTITY_PATTERNS)


def _jaccard(a: str, b: str) -> float:
    """
    Word-level Jaccard similarity — lightweight contradiction signal.
    Strips punctuation and numbers, keeps only alphabetic words ≥ 3 chars
    so 'retry=3' and 'retry=5' both normalize to 'retry' and match.
    """
    import re
    def _tokens(s: str) -> set:
        return {w for w in re.sub(r"[^a-z\s]", "", s.lower()).split() if len(w) >= 3}
    sa = _tokens(a)
    sb = _tokens(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _derive_source_type(rag_mode: Optional[str], repo_filter: Optional[str]) -> str:
    """
    Derive the context source type from rag_mode + repo_filter.

    Returns:
        "generic"  — rag_mode is "off"/""/ None AND no repo_filter
        "codebase" — rag_mode is "auto"/"on" AND repo_filter is set
        "kb"       — rag_mode is "auto"/"on" AND no repo_filter
        "unknown"  — legacy rows with NULL rag_mode that had a repo, etc.
    """
    _rm = (rag_mode or "").strip().lower()
    if _rm in ("", "off") and not repo_filter:
        return "generic"
    if _rm in ("auto", "on") and repo_filter:
        return "codebase"
    if _rm in ("auto", "on") and not repo_filter:
        return "kb"
    return "unknown"


# ============================================================
# L2 — SEMANTIC ANSWER CACHE
# ============================================================

def get_semantic_cached_answer(
    question: str,
    repo_filter: Optional[str] = None,
    user_id: Optional[str] = None,
    rag_mode: Optional[str] = None,
) -> Optional[dict]:
    """
    Search semantic_answer_cache for a similar past question.
    Returns {"answer", "similarity", "original_question"} or None.

    Identity queries (who am i, my role, etc.) MUST be scoped to user_id.
    If user_id is not provided for an identity query, bypass cache entirely
    to prevent cross-user data leaks.

    rag_mode: when "off" (Generic), only returns answers that were stored
    under Generic mode AND have no repo_filter, closing the
    `:repo IS NULL OR repo_filter = :repo` short-circuit that previously
    allowed KB/codebase answers to leak into Generic chats.
    """
    if not SEMANTIC_CACHE_ENABLED:
        return None

    # SECURITY: never serve identity answers from a shared/global cache
    if _is_identity_query(question) and not user_id:
        logger.debug("[SemanticCache] L2 SKIP — identity query without user_id")
        return None

    emb = _embed(question)
    if not emb:
        return None

    vec = _vec_str(emb)
    cutoff_days = SEMANTIC_CACHE_MAX_DAYS

    # Build user scope clause:
    # - identity queries: must match exact user_id
    # - other queries with user_id: match that user OR entries with no user (global)
    # - no user_id: global entries only
    if _is_identity_query(question):
        user_scope_clause = "AND user_id = :uid"
    elif user_id:
        user_scope_clause = "AND (user_id IS NULL OR user_id = :uid)"
    else:
        user_scope_clause = "AND user_id IS NULL"

    # Context isolation — 3-way: Generic ↛ KB ↛ Codebase ↛ Generic.
    # Each source type can only read cache entries written by the same type.
    # Legacy rows with NULL rag_mode are treated as "unknown" and are
    # excluded from typed reads (the IN ('auto','on') / = 'off' clauses
    # naturally exclude NULLs).
    _src = _derive_source_type(rag_mode, repo_filter)
    if _src == "generic":
        repo_clause = "AND repo_filter IS NULL"
        mode_clause = "AND rag_mode = 'off'"
    elif _src == "kb":
        repo_clause = "AND repo_filter IS NULL"
        mode_clause = "AND rag_mode IN ('auto', 'on')"
    elif _src == "codebase":
        repo_clause = "AND repo_filter = :repo"
        mode_clause = "AND rag_mode IN ('auto', 'on')"
    else:
        # Unknown / unset — permissive fallback (matches pre-fix behaviour)
        repo_clause = "AND (:repo IS NULL OR repo_filter = :repo)"
        mode_clause = ""

    try:
        with vector_engine.connect() as conn:
            rows = conn.execute(_text("""
                SELECT
                    question,
                    answer,
                    1 - (embedding <=> CAST(:vec AS vector)) AS similarity
                FROM ainxt.semantic_answer_cache
                WHERE
                    1 = 1
                    {repo_clause}
                    {user_scope}
                    {mode_clause}
                    AND created_at >= NOW() - INTERVAL '{cutoff_days} days'
                    AND 1 - (embedding <=> CAST(:vec AS vector)) >= :threshold
                ORDER BY embedding <=> CAST(:vec AS vector)
                LIMIT :limit
            """.format(
                cutoff_days=int(cutoff_days),
                user_scope=user_scope_clause,
                repo_clause=repo_clause,
                mode_clause=mode_clause,
            )), {
                "vec":       vec,
                "repo":      repo_filter,
                "uid":       user_id,
                "threshold": SEMANTIC_CACHE_THRESHOLD,
                "limit":     SEMANTIC_CACHE_MAX_RESULTS,
            }).fetchall()

        if not rows:
            return None

        best = rows[0]
        similarity = float(best.similarity)
        logger.info(
            f"[SemanticCache] L2 HIT  similarity={similarity:.3f}  "
            f"threshold={SEMANTIC_CACHE_THRESHOLD}  user={user_id}"
        )

        try:
            with vector_engine.connect() as conn:
                conn.execute(_text("""
                    UPDATE ainxt.semantic_answer_cache
                    SET hit_count = hit_count + 1, last_used = NOW()
                    WHERE question = :q
                """), {"q": best.question})
                conn.commit()
        except Exception:
            pass

        return {
            "answer":            best.answer,
            "similarity":        similarity,
            "original_question": best.question,
        }

    except Exception as e:
        logger.warning(f"[SemanticCache] L2 lookup failed: {e}")
        return None


def store_semantic_cached_answer(
    question: str,
    answer: str,
    repo_filter: Optional[str] = None,
    user_id: Optional[str] = None,
    confidence: float = 1.0,
    rag_mode: Optional[str] = None,
) -> None:
    """Store a Q&A pair in semantic_answer_cache. ON CONFLICT DO NOTHING (exact dedup).

    rag_mode: the chat's rag_mode at write time. Used by Generic read-side
    filtering to exclude KB/codebase-originated answers.
    """
    if not SEMANTIC_CACHE_ENABLED:
        return
    if not answer or not question:
        return

    emb = _embed(question)
    if not emb:
        return

    vec = _vec_str(emb)
    try:
        with vector_engine.connect() as conn:
            conn.execute(_text("""
                INSERT INTO ainxt.semantic_answer_cache
                    (question, answer, embedding, repo_filter, user_id, confidence, rag_mode)
                VALUES
                    (:q, :a, CAST(:vec AS vector), :repo, :uid, :conf, :rag_mode)
                ON CONFLICT DO NOTHING
            """), {
                "q":        question[:2000],
                "a":        answer,
                "vec":      vec,
                "repo":     repo_filter,
                "uid":      user_id,
                "conf":     confidence,
                "rag_mode": rag_mode,
            })
            conn.commit()
        logger.info(f"[SemanticCache] L2 STORED  user={user_id}  rag_mode={rag_mode}  q_len={len(question)}")
    except Exception as e:
        logger.warning(f"[SemanticCache] L2 store failed: {e}")


# ============================================================
# L3 — SEMANTIC MEMORY
# ============================================================

def get_semantic_memory(
    query: str,
    memory_type: Optional[str] = None,
    user_id: Optional[str] = None,
    department: Optional[str] = None,
    rag_mode: Optional[str] = None,
    source_repo: Optional[str] = None,
) -> list[dict]:
    """
    Retrieve learned patterns from semantic_memory relevant to the query.

    Scope model (layered — a user always sees all layers they belong to):
      org-level   → visible to everyone (scope_type='org')
      team-level  → visible to same department (scope_type='team', scope_id=department)
      user-level  → private to that user (scope_type='user', user_id=uid)

    Full WHERE clause:
        scope_type = 'org'
        OR (scope_type = 'team' AND scope_id = :dept)
        OR (scope_type = 'user' AND user_id = :uid)

    Ranking formula:
        similarity × 0.40  — how relevant is this memory to the query
        confidence × 0.25  — how trustworthy is this pattern
        hit_count  × 0.15  — how frequently has it been reinforced
        recency    × 0.20  — how recent (1.0=today → 0.0=90d ago)

    Context isolation — 3-way: Generic ↛ KB ↛ Codebase ↛ Generic.
    rag_mode + source_repo determines which memories are visible:
      Generic  (off, no repo)  → only rag_mode='off' AND source_repo IS NULL
      KB       (auto/on, no repo) → only rag_mode IN ('auto','on') AND source_repo IS NULL
      Codebase (auto/on, repo) → only rag_mode IN ('auto','on') AND source_repo = :repo
    Legacy rows with NULL rag_mode are excluded from typed reads.
    """
    if not SEMANTIC_MEMORY_ENABLED:
        return []

    emb = _embed(query)
    if not emb:
        return []

    vec = _vec_str(emb)
    cutoff_days = SEMANTIC_MEMORY_MAX_DAYS

    # ── Scope filter: always include org; add team+user layers when available ──
    scope_clauses = ["scope_type = 'org'"]
    scope_params: dict = {}
    if department:
        scope_clauses.append("(scope_type = 'team' AND scope_id = :dept)")
        scope_params["dept"] = department
    if user_id:
        scope_clauses.append("(scope_type = 'user' AND user_id = :uid)")
        scope_params["uid"] = user_id

    scope_filter = "AND (" + " OR ".join(scope_clauses) + ")"
    type_filter  = "AND type = :mtype" if memory_type else ""

    # Context isolation — 3-way: Generic ↛ KB ↛ Codebase ↛ Generic.
    # Each source type can only read memories written by the same type.
    # Legacy rows with NULL rag_mode are excluded from typed reads.
    _src = _derive_source_type(rag_mode, source_repo)
    if _src == "generic":
        mode_filter = "AND rag_mode = 'off' AND source_repo IS NULL"
    elif _src == "kb":
        mode_filter = "AND rag_mode IN ('auto', 'on') AND source_repo IS NULL"
    elif _src == "codebase":
        mode_filter = "AND rag_mode IN ('auto', 'on') AND source_repo = :source_repo"
        scope_params["source_repo"] = source_repo
    else:
        # Unknown / unset — permissive fallback (matches pre-fix behaviour)
        mode_filter = ""

    try:
        with vector_engine.connect() as conn:
            rows = conn.execute(_text("""
                SELECT
                    type,
                    summary,
                    content,
                    confidence,
                    hit_count,
                    1 - (embedding <=> CAST(:vec AS vector)) AS similarity
                FROM ainxt.semantic_memory
                WHERE
                    created_at >= NOW() - INTERVAL '{cutoff_days} days'
                    AND confidence >= :min_conf
                    AND 1 - (embedding <=> CAST(:vec AS vector)) >= :threshold
                    {scope_filter}
                    {type_filter}
                    {mode_filter}
                ORDER BY (
                    (1 - (embedding <=> CAST(:vec AS vector))) * 0.40 +
                    confidence * 0.25 +
                    LEAST(hit_count, 100)::float / 100.0 * 0.15 +
                    GREATEST(0.0,
                        1.0 - EXTRACT(EPOCH FROM (NOW() - created_at))
                              / ({cutoff_days}.0 * 86400)
                    ) * 0.20
                ) DESC
                LIMIT :limit
            """.format(
                cutoff_days=int(cutoff_days),
                scope_filter=scope_filter,
                type_filter=type_filter,
                mode_filter=mode_filter,
            )), {
                "vec":       vec,
                "min_conf":  SEMANTIC_MEMORY_MIN_CONFIDENCE,
                "threshold": SEMANTIC_MEMORY_THRESHOLD,
                "limit":     SEMANTIC_MEMORY_MAX_RESULTS,
                **({"mtype": memory_type} if memory_type else {}),
                **scope_params,
            }).fetchall()

        if not rows:
            return []

        results = [
            {
                "type":       r.type,
                "summary":    r.summary,
                "content":    r.content if isinstance(r.content, dict) else {},
                "confidence": float(r.confidence),
                "similarity": float(r.similarity),
                "hit_count":  int(r.hit_count),
            }
            for r in rows
        ]
        logger.info(
            f"[SemanticMemory] L3 HIT  results={len(results)}  "
            f"top_sim={results[0]['similarity']:.3f}  "
            f"top_hits={results[0]['hit_count']}  "
            f"dept={department or 'none'}  uid={user_id or 'none'}"
        )
        return results

    except Exception as e:
        logger.warning(f"[SemanticMemory] L3 lookup failed: {e}")
        return []


def _semantic_duplicate_exists(emb: list, memory_type: str) -> bool:
    """
    Check if a semantically near-identical memory already exists.
    Uses SEMANTIC_DEDUP_THRESHOLD (default 0.93) — higher than retrieval (0.75)
    so only near-copies are suppressed, not related-but-distinct memories.
    """
    vec = _vec_str(emb)
    try:
        with vector_engine.connect() as conn:
            row = conn.execute(_text("""
                SELECT 1
                FROM ainxt.semantic_memory
                WHERE type = :mtype
                  AND 1 - (embedding <=> CAST(:vec AS vector)) >= :threshold
                LIMIT 1
            """), {
                "mtype":     memory_type,
                "vec":       vec,
                "threshold": SEMANTIC_DEDUP_THRESHOLD,
            }).fetchone()
            if row:
                # Reinforce the existing similar memory's hit_count
                conn.execute(_text("""
                    UPDATE ainxt.semantic_memory
                    SET hit_count = hit_count + 1,
                        last_used = NOW()
                    WHERE type = :mtype
                      AND 1 - (embedding <=> CAST(:vec AS vector)) >= :threshold
                """), {
                    "mtype":     memory_type,
                    "vec":       vec,
                    "threshold": SEMANTIC_DEDUP_THRESHOLD,
                })
                conn.commit()
                return True
    except Exception:
        pass
    return False


def store_semantic_memory(
    memory_type: str,
    summary: str,
    content: dict,
    source: Optional[str] = None,
    confidence: float = 0.85,
    user_id: Optional[str] = None,
    scope_type: str = "org",
    scope_id: str = "global",
    rag_mode: Optional[str] = None,
    source_repo: Optional[str] = None,
) -> None:
    """
    Store a learned pattern in semantic_memory.

    Deduplication (two layers):
      1. Exact: ON CONFLICT(summary_hash) → increments hit_count by +1;
                confidence capped at CONFIDENCE_MAX (0.95) — repetition
                reinforces but can never reach certainty on its own.
      2. Semantic: embedding similarity ≥ SEMANTIC_DEDUP_THRESHOLD (0.93) →
                   reinforces the existing near-copy instead of inserting.

    Confidence cap prevents runaway confidence inflation from repeated
    patterns — repetition ≠ correctness.

    rag_mode / source_repo: context-isolation tags so Generic reads can
    exclude KB/codebase-originated memories.
    """
    if not SEMANTIC_MEMORY_ENABLED:
        return
    if not summary or confidence < SEMANTIC_MEMORY_MIN_CONFIDENCE:
        return

    emb = _embed(summary)
    if not emb:
        return

    # ── Semantic dedup (Priority 2) ───────────────────────────────────────────
    # Check embedding similarity BEFORE computing exact hash.
    # If a near-copy exists (sim ≥ 0.93), reinforce it and skip insert.
    if _semantic_duplicate_exists(emb, memory_type):
        logger.info(
            f"[SemanticMemory] L3 SEMANTIC DEDUP  type={memory_type}  "
            f"threshold={SEMANTIC_DEDUP_THRESHOLD}  reinforced existing"
        )
        return

    vec = _vec_str(emb)
    s_hash = _summary_hash(memory_type, summary)

    if scope_type == "user" and user_id and scope_id == "global":
        scope_id = user_id

    try:
        with vector_engine.connect() as conn:
            conn.execute(_text("""
                INSERT INTO ainxt.semantic_memory
                    (type, summary, content, embedding, source, confidence,
                     user_id, scope_type, scope_id, summary_hash,
                     rag_mode, source_repo)
                VALUES
                    (:mtype, :summary, CAST(:content AS jsonb),
                     CAST(:vec AS vector), :source, :conf,
                     :uid, :scope_type, :scope_id, :summary_hash,
                     :rag_mode, :source_repo)
                ON CONFLICT (summary_hash) WHERE summary_hash IS NOT NULL DO UPDATE SET
                    hit_count  = ainxt.semantic_memory.hit_count + 1,
                    last_used  = NOW(),
                    confidence = LEAST(
                        :conf_max,
                        ainxt.semantic_memory.confidence + 0.02
                    )
            """), {
                "mtype":        memory_type,
                "summary":      summary[:1000],
                "content":      json.dumps(content),
                "vec":          vec,
                "source":       source,
                "conf":         confidence,
                "uid":          user_id,
                "scope_type":   scope_type,
                "scope_id":     scope_id,
                "summary_hash": s_hash,
                "conf_max":     CONFIDENCE_MAX,
                "rag_mode":     rag_mode,
                "source_repo":  source_repo,
            })
            conn.commit()
        logger.info(
            f"[SemanticMemory] L3 STORED  type={memory_type}  "
            f"confidence={confidence:.2f}  scope={scope_type}/{scope_id}  "
            f"rag_mode={rag_mode}  source={source}"
        )
    except Exception as e:
        logger.warning(f"[SemanticMemory] L3 store failed: {e}")


# ============================================================
# PROMPT FORMATTING + CONTRADICTION DETECTION
# ============================================================

def format_memory_for_prompt(memories: list[dict]) -> str:
    """
    Format retrieved semantic memories as a prompt injection block.

    Contradiction detection: if ≥2 memories share >50% word overlap
    (same topic, potentially conflicting details), a warning is appended
    so the LLM treats them with appropriate skepticism rather than blindly
    applying conflicting advice.
    """
    if not memories:
        return ""

    lines = ["Relevant learnings from past runs (use as context, not as gospel):"]
    for m in memories:
        conf_pct = int(m["confidence"] * 100)
        hits = m.get("hit_count", 0)
        hit_note = f", used {hits}x" if hits > 1 else ""
        lines.append(f"- [{m['type']}] {m['summary']} (confidence: {conf_pct}%{hit_note})")

    # ── Contradiction detection ───────────────────────────────────────────────
    if len(memories) >= 2:
        conflict_pairs = []
        for i in range(len(memories)):
            for j in range(i + 1, len(memories)):
                sim = _jaccard(memories[i]["summary"], memories[j]["summary"])
                if sim > 0.50:
                    conflict_pairs.append((i, j, sim))

        if conflict_pairs:
            lines.append(
                "\n⚠️  Potential conflict: some learnings above overlap on the same topic "
                "but may give different guidance. Verify before applying."
            )

    return "\n".join(lines)
