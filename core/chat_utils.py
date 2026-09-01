# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CHAT UTILITIES — standalone helpers shared by gateway.py
# and chat_worker.py.
#
# Extracted so workers never import gateway.py (which triggers
# OrchestratorAgent.__init__, Redis connections, and model
# loading as side-effects at import time).
# ============================================================

import re
import hashlib

from core.logger import logger

# ── PII masking patterns (mirrors pii_detector.py — keep in sync) ────────────

_MASK_CARD16_RE = re.compile(r'\b\d{16}\b')
_MASK_PHONE_RE  = re.compile(
    r'(?<!\d)'
    r'(?:'
        r'(?:\+|00)\d{1,3}'
        r'[\s\-\.]?(?:\(?\d{1,4}\)?[\s\-\.]?)?'
        r'\d{3,6}[\s\-\.]?\d{3,6}'
        r'|'
        r'[6-9]\d{9}'
    r')'
    r'(?!\d)',
    re.ASCII,
)
_MASK_EMAIL_RE  = re.compile(r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b')
_MASK_IPAN_RE   = re.compile(r'\b[A-Z]{5}[0-9]{4}[A-Z]\b', re.IGNORECASE)


def mask_pii(text: str) -> str:
    """Mask PII before forwarding text to an LLM or writing to logs."""
    if not text:
        return text
    text = _MASK_CARD16_RE.sub("XXXX-XXXX-XXXX-XXXX", text)
    text = _MASK_PHONE_RE.sub("XXXXXXXXXX", text)
    text = _MASK_EMAIL_RE.sub("***@***.***", text)
    text = _MASK_IPAN_RE.sub("XXXXX0000X", text)
    return text


# ── Repo detection ────────────────────────────────────────────────────────────

_REPO_CACHE: list | None = None


def detect_repo(question: str) -> str | None:
    """
    Detect which repo a question is about by matching tokens against
    repos registered in pgvector (document_embeddings.repo column).
    Falls back to None — caller decides whether to use RAG.
    """
    if not question:
        return None

    global _REPO_CACHE
    try:
        if not _REPO_CACHE:
            from db.database import SessionLocal
            from sqlalchemy import text as _sql
            db = SessionLocal()
            try:
                rows = db.execute(
                    _sql("SELECT DISTINCT repo FROM document_embeddings WHERE repo NOT LIKE 'docs_kb:%'")
                ).fetchall()
                _REPO_CACHE = [r[0].lower() for r in rows if r[0]]
                logger.info(f"detect_repo: loaded {len(_REPO_CACHE)} repos from pgvector")
            finally:
                db.close()

        if not _REPO_CACHE:
            return None

        tokens = set(re.findall(r"[a-zA-Z0-9_\-]{3,}", question.lower()))
        for repo in _REPO_CACHE:
            if repo in tokens:
                logger.info(f"detect_repo: matched {repo!r}")
                return repo
        return None
    except Exception as e:
        logger.error(f"detect_repo failed: {e}")
        return None


# ── Cache key ─────────────────────────────────────────────────────────────────

# Buckets that produce DIFFERENT answers for the same question, so the L1 cache
# must isolate them. Without rag_mode in the key, a Generic-tab response was
# being served for the same prompt asked from the KB tab (cross-mode bleed),
# because both flows leave repo_filter=None when no project is selected.
#
# Bucketing:
#   "off"     - Generic (no retrieval; LLM general knowledge only)
#   "kb"      - KB grounded (rag_mode in {auto, on} with NO repo_filter)
#   "code"    - Codebase grounded (repo_filter set)
# Other / legacy values fall into "off" so we never re-serve them as KB hits.

def _cache_bucket(repo_filter: str | None, rag_mode: str | None) -> str:
    if repo_filter:
        return "code"
    rm = (rag_mode or "").strip().lower()
    if rm in ("auto", "on"):
        return "kb"
    return "off"


def chat_cache_key(
        question: str,
        repo_filter: str | None,
        rag_mode: str | None = None,
) -> str:
    """
    Build the L1 Redis cache key for an Ask request.

    rag_mode is a NEW parameter (default None for backward compatibility) but
    callers SHOULD pass it. Without it the function still works — it just
    cannot distinguish Generic vs KB requests, which is the bug we fixed.
    Version is bumped to v4 so any v3 entries cached before this change are
    treated as misses on the next request and naturally roll off.
    """
    repo   = repo_filter or "global"
    bucket = _cache_bucket(repo_filter, rag_mode)
    raw    = f"ask:v4:{bucket}:{repo}:{question.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()
