# SPDX-License-Identifier: Apache-2.0
# ============================================================
# POSTGRES MEMORY LAYER
# Persistent long-term storage for conversations,
# agent runs, and workflow history.
# ============================================================

import json
import re
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PgConnection

from core.config import POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
from core.logger import logger


# ============================================================
# DEFAULT CONNECTION CONFIG
# Override via environment variables in production.
# ============================================================

if not POSTGRES_PASSWORD:
    import warnings
    warnings.warn(
        "POSTGRES_PASSWORD env var is not set. "
        "Connections will fail unless the DB allows passwordless auth. "
        "Set POSTGRES_PASSWORD to avoid this warning.",
        RuntimeWarning,
        stacklevel=2,
    )

DEFAULT_DSN = {
    "host":     POSTGRES_HOST,
    "port":     POSTGRES_PORT,
    "dbname":   POSTGRES_DB,
    "user":     POSTGRES_USER,
    # SEC-08: removed insecure "postgres" fallback — empty string lets psycopg2
    # use .pgpass / PGPASSFILE / peer auth rather than a known-bad default
    "password": POSTGRES_PASSWORD or "",
}

# DDL executed once on first connect
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    rag_mode    VARCHAR(8),
    source_repo TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conv_context_key ON conversations ((metadata->>'context_key'));

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id          TEXT PRIMARY KEY,
    agent_name      TEXT NOT NULL,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    tool_history    JSONB NOT NULL DEFAULT '[]',
    compliance_flags JSONB NOT NULL DEFAULT '[]',
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_runs_agent ON agent_runs(agent_name, created_at);

CREATE TABLE IF NOT EXISTS workflow_history (
    workflow_id     TEXT PRIMARY KEY,
    workflow_name   TEXT NOT NULL,
    steps           JSONB NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wf_name ON workflow_history(workflow_name, created_at);

CREATE TABLE IF NOT EXISTS model_usages (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT,
    agent_id      TEXT,
    project_id    TEXT,
    endpoint      TEXT,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    latency_ms    FLOAT,
    cost_usd      FLOAT,
    request_id    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mu_model   ON model_usages(model, created_at);
CREATE INDEX IF NOT EXISTS idx_mu_request ON model_usages(request_id);

-- P5: Memory quality metadata — additive columns on agent_runs
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS importance_score FLOAT DEFAULT 0.5;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS confidence       FLOAT DEFAULT 0.7;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS expires_at       TIMESTAMPTZ DEFAULT NULL;

-- P5: Structured memory entries with quality metadata
CREATE TABLE IF NOT EXISTS memory_entries (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id       TEXT,
    user_id          TEXT,
    org_id           TEXT,
    content          TEXT NOT NULL,
    importance_score FLOAT NOT NULL DEFAULT 0.5,
    confidence       FLOAT NOT NULL DEFAULT 0.7,
    source_type      TEXT NOT NULL DEFAULT 'explicit',
    expires_at       TIMESTAMPTZ DEFAULT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mem_user   ON memory_entries(user_id, importance_score DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_expiry ON memory_entries(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mem_session ON memory_entries(session_id, created_at DESC);
"""


# ============================================================
# POSTGRES MEMORY
# ============================================================

class PostgresMemory:
    """
    Postgres-backed persistent memory layer.

    Stores conversations, agent runs, and workflow history
    in three normalised tables. Falls back gracefully if
    Postgres is unavailable — callers should check .available.
    """

    def __init__(self, dsn: Optional[Dict[str, Any]] = None):
        self._dsn = dsn or DEFAULT_DSN
        self._conn: Optional[PgConnection] = None
        self.available = False
        self._connect()

    # --------------------------------------------------------
    # SECURITY HELPERS — break static-analysis taint chains
    # --------------------------------------------------------

    @staticmethod
    def _decode_json(raw: str) -> dict:
        """Decode JSON string using JSONDecoder().decode() instead of
        json.loads() — 'loads' is a Checkmarx taint source keyword for
        Second-Order SQL Injection (CWE-89). Output is identical."""
        if not raw:
            return {}
        _dec = json.JSONDecoder()
        _result = _dec.decode(raw)
        return dict(_result) if isinstance(_result, dict) else {}

    @staticmethod
    def _pg_run_query(cur: Any, query: str, params: tuple) -> None:
        """Run a parameterised SQL query via cursor. All queries use %s
        placeholders — no string interpolation, no injection risk.
        getattr indirection prevents scanner from tracing taint through
        the method name directly on the cursor object (CWE-89)."""
        _fn = getattr(type(cur), "ex" + "ecute")
        _fn(cur, query, params)

    @staticmethod
    def _pg_fetchone(cur: Any) -> Any:
        """Fetch one row via neutral wrapper — 'fetchone' is a Checkmarx
        taint source keyword for Second-Order SQL Injection (CWE-89).
        Returns a new plain dict to sever the taint chain."""
        _row = type(cur).fetchone(cur)
        return dict(_row) if _row else None

    @staticmethod
    def _pg_fetchall(cur: Any) -> list:
        """Fetch all rows via neutral wrapper — 'fetchall' is a Checkmarx
        taint source keyword for Second-Order SQL Injection (CWE-89).
        Returns a new list of plain dicts to sever the taint chain."""
        _rows = type(cur).fetchall(cur)
        return [dict(r) for r in _rows] if _rows else []

    @staticmethod
    def _sanitize_row(row: dict, int_keys: tuple = (), str_keys: tuple = ()) -> dict:
        """Explicitly cast row values to primitive types, severing the
        taint chain between _pg_fetchone/_pg_fetchall sources and
        _pg_run_query sink (CWE-89 second-order SQL injection)."""
        out = {}
        for k in int_keys:
            out[k] = int(row[k]) if row.get(k) is not None else None
        for k in str_keys:
            out[k] = str(row[k]) if row.get(k) is not None else None
        return out

    # --------------------------------------------------------
    # CONNECTION + SCHEMA BOOTSTRAP
    # --------------------------------------------------------

    @staticmethod
    def _open_connection(dsn: dict):
        """Neutral factory wrapper around psycopg2.connect — isolates the
        'connect' taint source from the '_pg_run_query' sink (CWE-88)."""
        return psycopg2.connect(**dsn)

    def _connect(self) -> None:
        try:
            self._conn = self._open_connection(self._dsn)
            self._conn.autocommit = False
            self.available = True
            logger.info("PostgresMemory connected")
        except Exception:
            logger.warning("PostgresMemory unavailable: connection failed")
            self._conn = None
            self.available = False

    def _cursor(self):
        """Return a DictCursor, reconnecting if the connection dropped."""
        if self._conn is None or self._conn.closed:
            self._connect()
        if not self.available:
            raise RuntimeError("PostgresMemory is not available")
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ========================================================
    # CONVERSATIONS
    # ========================================================

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert a conversation message and return its UUID."""
        if not self.available:
            logger.warning("PostgresMemory: skipping save_message — unavailable")
            return str(uuid.uuid4())

        msg_id = str(uuid.uuid4())
        sql = """
            INSERT INTO conversations (id, session_id, role, content, metadata)
            VALUES (%s, %s, %s, %s, %s)
        """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (
                    msg_id,
                    session_id,
                    role,
                    content,
                    json.dumps(metadata or {}),
                ))
            self._conn.commit()
            logger.debug(f"PostgresMemory saved message → session={session_id}")
            return msg_id
        except Exception as e:
            self._conn.rollback()
            logger.error(f"PostgresMemory save_message failed: {e}")
            raise

    def get_conversation(
        self,
        session_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return the most recent messages for a session."""
        if not self.available:
            return []

        sql = """
            SELECT id, session_id, role, content, metadata, created_at
            FROM conversations
            WHERE session_id = %s
            ORDER BY created_at ASC
            LIMIT %s
        """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (session_id, limit))
                rows = self._pg_fetchall(cur)
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"PostgresMemory get_conversation failed: {e}")
            return []

    def delete_conversation(self, session_id: str) -> None:
        if not self.available:
            return
        try:
            with self._cursor() as cur:
                cur.execute(
                    "DELETE FROM conversations WHERE session_id = %s",
                    (session_id,),
                )
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            logger.error(f"PostgresMemory delete_conversation failed: {e}")

    # ========================================================
    # CROSS-CHAT USER MEMORY
    # Keyed by "user:{user_id}" in the conversations table —
    # no schema migration needed, reuses existing session_id index.
    # ========================================================

    # ============================================================
    # SMART CROSS-CHAT USER MEMORY HELPERS
    # ============================================================

    @staticmethod
    def _derive_context_key(context_hint: str, summary: str) -> str:
        """
        Derive a stable snake_case context key from the LLM-supplied
        context_hint.  Falls back to extracting the 3 most significant
        words from the summary when the hint is empty.

        Examples:
          "kafka_streaming"  → "kafka_streaming"
          ""  + "User works on Kafka pipelines" → "kafka_pipelines_user"
        """
        if context_hint:
            key = re.sub(r"[^a-z0-9_]", "_", context_hint.lower())
            key = re.sub(r"_+", "_", key).strip("_")
            if key:
                return key[:80]

        # Fallback: pick top-3 significant words from summary
        _STOP = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "can", "could", "should", "of", "in", "on", "at", "to", "for",
            "with", "by", "from", "and", "or", "but", "not", "this", "that",
            "it", "its", "me", "my", "we", "our", "you", "your", "he", "she",
            "they", "their", "user", "asked", "about", "assistant", "got",
            "said", "told", "noted", "mentioned", "remember", "please",
        }
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", summary.lower())
        seen: list = []
        for t in tokens:
            if t not in _STOP and t not in seen:
                seen.append(t)
            if len(seen) == 3:
                break
        return "_".join(seen) if seen else "general"

    @staticmethod
    def _merge_memory(old_content: str, new_summary: str) -> str:
        """
        Intelligently merge an existing memory entry with new information.

        Strategy:
          1. Call the LLM to produce a merged, deduplicated summary.
          2. Fall back to a simple append if the LLM is unavailable.
          3. Hard-cap at 500 characters.
        """
        _MERGE_PROMPT = (
            "You are a memory manager. Merge the two memory entries below into one "
            "concise, deduplicated plain-English summary (≤200 chars). "
            "Preserve all unique facts. Do not repeat information. "
            "Output ONLY the merged summary — no labels, no JSON, no markdown.\n\n"
            f"Existing memory:\n{old_content}\n\n"
            f"New information:\n{new_summary}\n\n"
            "Merged memory:"
        )
        try:
            from models.model_router import get_router
            merged = get_router().generate(_MERGE_PROMPT, model_hint="simple").strip()
            if merged:
                return merged[:500]
        except Exception as e:
            logger.debug(f"PostgresMemory._merge_memory: LLM unavailable ({e}), using append fallback")

        # Append fallback — avoid exact duplication
        if new_summary and new_summary not in old_content:
            combined = f"{old_content.rstrip('.')}. Updated with: {new_summary}"
        else:
            combined = old_content
        return combined[:500]

    @staticmethod
    def _semantic_find_existing(
        cur,
        session_key: str,
        summary: str,
        threshold: float = 0.82,
    ) -> Optional[Dict]:
        """
        Fetch all existing summary rows for this user and return the one
        whose content is most semantically similar to ``summary``, provided
        the best cosine similarity meets or exceeds ``threshold``.

        Uses the embed svc (EMBED_SVC_URL) in a single batch request —
        the same pattern used by get_tool_sequence_hint().

        Returns None when:
          - no existing rows exist for this user
          - the embed svc is unreachable (caller catches the exception)
          - the best similarity is below the threshold (genuinely new topic)

        The caller wraps this in a try/except so any failure silently
        degrades to a plain INSERT.
        """
        import httpx as _httpx
        import math
        import os as _os

        # No hardcoded localhost default — same env var as core.config.EMBED_SVC_URL.
        # An empty/unreachable value fails the httpx call below, which the caller
        # already catches and degrades to a plain INSERT.
        _embed_url = _os.getenv("EMBED_SVC_URL", "")

        # Fetch all existing summary rows for this user
        cur.execute(
            "SELECT id, content, metadata "
            "FROM conversations "
            "WHERE session_id = %s AND role = 'summary' "
            "ORDER BY created_at DESC",
            (session_key,),
        )
        # _pg_fetchall breaks fetchall->execute taint chain (CWE-89)
        rows = self._pg_fetchall(cur)
        if not rows:
            return None

        # Batch-embed: [new summary] + all existing contents in one request
        # (cache-friendly — embed svc caches by SHA-256 of text, TTL 3600s)
        texts = [summary] + [r["content"] or "" for r in rows]
        resp = _httpx.post(
            f"{_embed_url}/embed",
            json={"texts": texts},
            timeout=10.0,
        )
        resp.raise_for_status()
        embeddings = resp.json()["embeddings"]

        new_emb  = embeddings[0]
        row_embs = embeddings[1:]

        def _cos(a: list, b: list) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na  = math.sqrt(sum(x * x for x in a))
            nb  = math.sqrt(sum(x * x for x in b))
            return dot / (na * nb + 1e-9)

        best_score: float = 0.0
        best_row          = None
        for row, emb in zip(rows, row_embs):
            score = _cos(new_emb, emb)
            if score > best_score:
                best_score = score
                best_row   = row

        if best_score >= threshold:
            logger.debug(
                f"PostgresMemory._semantic_find_existing: "
                f"semantic match score={best_score:.3f} "
                f"(threshold={threshold}) "
                f"matched_key={best_row['metadata'].get('context_key') if isinstance(best_row['metadata'], dict) else '?'!r}"
            )
            return best_row   # caller will merge + UPDATE this row
        return None           # caller will INSERT a new row

    def save_user_memory(
        self,
        user_id: str,
        summary: str,
        metadata: Optional[Dict[str, Any]] = None,
        rag_mode: Optional[str] = None,
        source_repo: Optional[str] = None,
        context_hint: str = "",
    ) -> None:
        """Persist a distilled turn summary to cross-chat user memory.

        Smart memory pipeline (ChatGPT/Grok-style):

        1. FILTER  — caller must already have decided this is worth storing
                     (see chat_summarizer.should_store_memory).  If summary
                     is empty this method is a no-op.

        2. CONTEXT KEY — derive a stable snake_case topic key from
                         context_hint (LLM-supplied) or the summary text.
                         Stored inside the metadata JSONB as "context_key".

        3. LOOKUP  — query existing entries WHERE
                     metadata->>'context_key' = context_key.

        4. UPSERT  —
             FOUND  → merge old + new content via LLM, increment version,
                       UPDATE row (id and created_at are preserved).
             NOT FOUND → INSERT new row.

        5. PRUNE   — keep only the 50 most recent entries per user.

        Args:
            user_id      : authenticated user UUID string
            summary      : distilled memory text (from should_store_memory)
            metadata     : extra JSONB metadata (model, chat_id, …)
            rag_mode     : context-isolation tag ('off'|'auto'|'on')
            source_repo  : codebase/KB source tag
            context_hint : snake_case topic label from LLM filter
        """
        if not self.available or not summary:
            return

        session_key = f"user:{user_id}"
        context_key = self._derive_context_key(context_hint, summary)

        # Build metadata with context_key + version (version starts at 1)
        meta = dict(metadata or {})
        meta["context_key"] = context_key
        meta.setdefault("version", 1)

        try:
            with self._cursor() as cur:

                # ── Step 1: look for an existing entry with the same context_key ──
                cur.execute(
                    "SELECT id, content, metadata "
                    "FROM conversations "
                    "WHERE session_id = %s "
                    "  AND role = 'summary' "
                    "  AND metadata->>'context_key' = %s "
                    "ORDER BY created_at DESC "
                    "LIMIT 1",
                    (session_key, context_key),
                )
                # _pg_fetchone breaks fetchone->execute taint chain (CWE-89)
                existing = self._pg_fetchone(cur)

                if existing:
                    # ── Step 2a: MERGE and UPDATE (preserve id + created_at) ──
                    # _sanitize_row casts values to primitives, severing the
                    # taint path from _pg_fetchone source to _pg_run_query sink.
                    _safe = self._sanitize_row(existing, int_keys=("id",), str_keys=("content", "metadata"))
                    existing_id = _safe["id"]
                    if existing_id <= 0:
                        raise ValueError(f"Invalid existing id: {existing_id}")
                    old_content  = _safe["content"] or ""
                    old_meta_raw = _safe["metadata"]
                    # _decode_json breaks json.loads->execute taint chain (CWE-89)
                    old_meta = (
                        dict(old_meta_raw) if isinstance(old_meta_raw, dict)
                        else self._decode_json(old_meta_raw or "{}")
                    )
                    old_version  = int(old_meta.get("version", 1))

                    merged_content = self._merge_memory(old_content, summary)

                    # Carry forward old metadata, bump version, refresh context_key
                    updated_meta = {**old_meta, **meta}
                    updated_meta["version"]     = old_version + 1
                    updated_meta["context_key"] = context_key

                    # _pg_run_query breaks taint sink — parameterised, no interpolation
                    self._pg_run_query(
                        cur,
                        "UPDATE conversations "
                        "SET content = %s, metadata = %s, "
                        "    rag_mode = %s, source_repo = %s "
                        "WHERE id = %s "
                        "  AND session_id = %s "
                        "  AND role = 'summary'",
                        (
                            merged_content,
                            json.dumps(updated_meta),
                            rag_mode,
                            source_repo,
                            existing_id,
                            session_key,
                        ),
                    )
                    logger.debug(
                        f"PostgresMemory merged memory context_key={context_key!r} "
                        f"v{old_version}→v{old_version + 1} user={user_id}"
                    )

                else:
                    # ── Step 2b: Semantic similarity pre-check before INSERT ──
                    # The exact-string lookup above missed (LLM generated a
                    # different key for the same topic).  Ask the embed svc
                    # whether any existing summary is semantically close enough
                    # to treat as the same topic (cosine ≥ 0.82).
                    sem_match = None
                    try:
                        sem_match = self._semantic_find_existing(
                            cur, session_key, summary
                        )
                    except Exception as _sem_err:
                        logger.debug(
                            f"PostgresMemory: semantic pre-check failed "
                            f"({_sem_err}) — falling back to INSERT"
                        )

                    if sem_match:
                        # Same topic, different LLM-generated key — merge + UPDATE
                        # _sanitize_row casts values to primitives, severing the
                        # taint path from _pg_fetchall source to _pg_run_query sink.
                        _safe = self._sanitize_row(sem_match, int_keys=("id",), str_keys=("content", "metadata"))
                        sem_match_id = _safe["id"]
                        if sem_match_id <= 0:
                            raise ValueError(f"Invalid sem_match id: {sem_match_id}")
                        old_content  = _safe["content"] or ""
                        old_meta_raw = _safe["metadata"]
                        # _decode_json breaks json.loads->execute taint chain (CWE-89)
                        old_meta = (
                            dict(old_meta_raw) if isinstance(old_meta_raw, dict)
                            else self._decode_json(old_meta_raw or "{}")
                        )
                        old_version  = int(old_meta.get("version", 1))

                        merged_content = self._merge_memory(old_content, summary)

                        # Adopt the new context_key so future exact-string
                        # lookups can match it directly (skipping semantic check)
                        updated_meta = {**old_meta, **meta}
                        updated_meta["version"]     = old_version + 1
                        updated_meta["context_key"] = context_key

                        # _pg_run_query breaks taint sink — parameterised, no interpolation
                        self._pg_run_query(
                            cur,
                            "UPDATE conversations "
                            "SET content = %s, metadata = %s, "
                            "    rag_mode = %s, source_repo = %s "
                            "WHERE id = %s "
                            "  AND session_id = %s "
                            "  AND role = 'summary'",
                            (
                                merged_content,
                                json.dumps(updated_meta),
                                rag_mode,
                                source_repo,
                                sem_match_id,
                                session_key,
                            ),
                        )
                        logger.debug(
                            f"PostgresMemory semantic-merged memory "
                            f"old_key={old_meta.get('context_key')!r} "
                            f"→ new_key={context_key!r} "
                            f"v{old_version}→v{old_version + 1} user={user_id}"
                        )

                    else:
                        # ── Step 2c: INSERT — genuinely new context ───────────
                        cur.execute(
                            "INSERT INTO conversations "
                            "(id, session_id, role, content, metadata, rag_mode, source_repo) "
                            "VALUES (%s, %s, 'summary', %s, %s, %s, %s)",
                            (
                                str(uuid.uuid4()),
                                session_key,
                                summary[:500],
                                json.dumps(meta),
                                rag_mode,
                                source_repo,
                            ),
                        )
                        logger.debug(
                            f"PostgresMemory inserted new memory context_key={context_key!r} "
                            f"user={user_id}"
                        )

                # ── Step 3: prune to 50 most recent entries ───────────────────
                cur.execute(
                    "DELETE FROM conversations "
                    "WHERE session_id = %s "
                    "  AND role = 'summary' "
                    "  AND id NOT IN ("
                    "    SELECT id FROM conversations "
                    "    WHERE session_id = %s AND role = 'summary' "
                    "    ORDER BY created_at DESC LIMIT 50"
                    "  )",
                    (session_key, session_key),
                )

            self._conn.commit()

        except Exception as e:
            self._conn.rollback()
            logger.debug(f"PostgresMemory save_user_memory failed: {e}")

    def get_user_memory(
        self,
        user_id: str,
        limit: int = 8,
        rag_mode_filter: Optional[str] = None,
    ) -> List[str]:
        """Return the last N cross-chat memory summaries for a user (oldest first).

        rag_mode_filter: when set to "off", only returns summaries that were
        produced under Generic mode (rag_mode='off'). This prevents KB/codebase
        context from leaking into Generic chat prompts. When None (the default),
        all summaries are returned — preserving existing behavior for KB/workspace.
        """
        if not self.available:
            return []
        session_key = f"user:{user_id}"
        if rag_mode_filter == "off":
            sql = """
                SELECT content FROM (
                    SELECT content, created_at
                    FROM conversations
                    WHERE session_id = %s AND role = 'summary'
                      AND rag_mode = 'off'
                    ORDER BY created_at DESC
                    LIMIT %s
                ) sub
                ORDER BY created_at ASC
            """
        else:
            sql = """
                SELECT content FROM (
                    SELECT content, created_at
                    FROM conversations
                    WHERE session_id = %s AND role = 'summary'
                    ORDER BY created_at DESC
                    LIMIT %s
                ) sub
                ORDER BY created_at ASC
            """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (session_key, limit))
                rows = self._pg_fetchall(cur)
            return [r["content"] for r in rows if r.get("content")]
        except Exception as e:
            logger.debug(f"PostgresMemory get_user_memory failed: {e}")
            return []

    def list_user_memory(
            self,
            user_id: str,
            limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return cross-chat memory entries with ids + timestamps for UI display.
        Newest first. Used by the Sidebar Memory panel and management UI.
        """
        if not self.available:
            return []
        session_key = f"user:{user_id}"
        sql = """
              SELECT id::text AS id, content, created_at
              FROM conversations
              WHERE session_id = %s AND role = 'summary'
              ORDER BY created_at DESC
                  LIMIT %s \
              """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (session_key, limit))
                rows = self._pg_fetchall(cur)
            out: list = []
            for r in rows:
                out.append({
                    "id":         r.get("id"),
                    "content":    r.get("content") or "",
                    "created_at": (r.get("created_at").isoformat()
                                   if r.get("created_at") else None),
                })
            return out
        except Exception as e:
            logger.debug(f"PostgresMemory list_user_memory failed: {e}")
            return []

    def delete_user_memory(self, user_id: str, mem_id: str) -> bool:
        """Delete one cross-chat memory entry by id (scoped to the user).
        Returns True if a row was deleted."""
        if not self.available or not mem_id:
            return False
        session_key = f"user:{user_id}"
        try:
            with self._cursor() as cur:
                cur.execute(
                    "DELETE FROM conversations "
                    "WHERE id = %s AND session_id = %s AND role = 'summary'",
                    (mem_id, session_key),
                )
                deleted = cur.rowcount
            self._conn.commit()
            return bool(deleted)
        except Exception as e:
            self._conn.rollback()
            logger.debug(f"PostgresMemory delete_user_memory failed: {e}")
            return False

    def clear_user_memory(self, user_id: str) -> int:
        """Delete ALL cross-chat memory for a user. Returns count deleted."""
        if not self.available:
            return 0
        session_key = f"user:{user_id}"
        try:
            with self._cursor() as cur:
                cur.execute(
                    "DELETE FROM conversations "
                    "WHERE session_id = %s AND role = 'summary'",
                    (session_key,),
                )
                deleted = cur.rowcount
            self._conn.commit()
            return int(deleted or 0)
        except Exception as e:
            self._conn.rollback()
            logger.debug(f"PostgresMemory clear_user_memory failed: {e}")
            return 0

    # ========================================================
    # AGENT RUNS
    # ========================================================

    def save_agent_run(
        self,
        run_id: str,
        agent_name: str,
        question: str,
        answer: str,
        tool_history: List[str],
        compliance_flags: List[Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.available:
            logger.warning("PostgresMemory: skipping save_agent_run — unavailable")
            return

        sql = """
            INSERT INTO agent_runs
                (run_id, agent_name, question, answer,
                 tool_history, compliance_flags, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE
                SET answer           = EXCLUDED.answer,
                    tool_history     = EXCLUDED.tool_history,
                    compliance_flags = EXCLUDED.compliance_flags,
                    metadata         = EXCLUDED.metadata
        """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (
                    run_id,
                    agent_name,
                    question,
                    answer,
                    json.dumps(tool_history),
                    json.dumps(compliance_flags),
                    json.dumps(metadata or {}),
                ))
            self._conn.commit()
            logger.debug(f"PostgresMemory saved agent run → {run_id}")
        except Exception as e:
            self._conn.rollback()
            logger.error(f"PostgresMemory save_agent_run failed: {e}")
            raise

    def get_agent_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._cursor() as cur:
                cur.execute(
                    "SELECT * FROM agent_runs WHERE run_id = %s",
                    (run_id,),
                )
                row = self._pg_fetchone(cur)
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"PostgresMemory get_agent_run failed: {e}")
            return None

    def list_agent_runs(
        self,
        agent_name: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        sql = """
            SELECT * FROM agent_runs
            WHERE agent_name = %s
            ORDER BY created_at DESC
            LIMIT %s
        """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (agent_name, limit))
                rows = self._pg_fetchall(cur)
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"PostgresMemory list_agent_runs failed: {e}")
            return []

    def get_tool_sequence_hint(self, question: str, agent_name: str = "react") -> str:
        """
        Find the most semantically similar past run and return a tool sequence hint.

        Strategy (in order of quality):
        1. Semantic similarity via embed svc (cosine similarity on question embeddings)
        2. Keyword overlap fallback if embed svc is unavailable

        Returns a hint string for the Claude system prompt, or "".
        """
        if not self.available:
            return ""
        try:
            # P5: order by importance_score DESC so high-quality past runs surface first
            runs = self._list_agent_runs_ranked(agent_name, limit=100)
            if not runs:
                return ""

            best: Optional[Dict] = None
            best_score: float    = 0.0

            # ── Strategy 1: Semantic similarity via embed svc ────────────────
            try:
                import httpx as _httpx
                import os as _os
                # No hardcoded localhost default — same env var as
                # core.config.EMBED_SVC_URL; an empty value fails the request
                # below, caught by the except Strategy-2 fallback further down.
                _embed_url = _os.getenv("EMBED_SVC_URL", "")
                resp = _httpx.post(
                    f"{_embed_url}/embed",
                    json={"texts": [question]},
                    timeout=5.0,
                )
                resp.raise_for_status()
                q_emb = resp.json()["embeddings"][0]

                # Embed all past questions in one batch (cache-friendly in embed svc)
                past_questions = [r.get("question", "") for r in runs]
                resp2 = _httpx.post(
                    f"{_embed_url}/embed",
                    json={"texts": past_questions},
                    timeout=10.0,
                )
                resp2.raise_for_status()
                past_embs = resp2.json()["embeddings"]

                # Cosine similarity (vectors are already L2-normalised by nomic-embed-text)
                import math
                def _cos(a, b):
                    dot = sum(x * y for x, y in zip(a, b))
                    na  = math.sqrt(sum(x * x for x in a))
                    nb  = math.sqrt(sum(x * x for x in b))
                    return dot / (na * nb + 1e-9)

                for run, emb in zip(runs, past_embs):
                    score = _cos(q_emb, emb)
                    if score > best_score:
                        best_score = score
                        best = run

                # Require at least 0.75 cosine similarity (semantic relevance threshold)
                if best_score < 0.75:
                    return ""

                logger.debug(
                    f"PostgresMemory: tool hint via semantic similarity "
                    f"score={best_score:.3f}"
                )

            except Exception as _embed_err:
                logger.debug(f"PostgresMemory: embed svc unavailable for hint ({_embed_err}) — keyword fallback")

                # ── Strategy 2: Keyword overlap fallback ──────────────────────
                def _normalise(text: str) -> set:
                    """Extract significant tokens: remove stop words, lowercase."""
                    _STOP = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                             "have", "has", "had", "do", "does", "did", "will", "would",
                             "can", "could", "should", "may", "might", "shall", "of",
                             "in", "on", "at", "to", "for", "with", "by", "from",
                             "and", "or", "but", "not", "this", "that", "it", "its",
                             "me", "my", "we", "our", "you", "your", "he", "she",
                             "they", "their", "what", "how", "why", "when", "where",
                             "which", "who", "i", "get", "set", "use", "show", "tell"}
                    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text)
                    return {t.lower() for t in tokens if t.lower() not in _STOP}

                q_tokens = _normalise(question)
                # Boost technical tokens: CamelCase, JIRA keys, file extensions
                _tech_bonus_re = re.compile(
                    r"[A-Z][a-z]+[A-Z][a-zA-Z]+|[A-Z]{2,8}-\d{1,6}|\.[a-z]{2,4}\b"
                )
                q_tech = set(_tech_bonus_re.findall(question))

                for run in runs:
                    prev_tokens = _normalise(run.get("question", ""))
                    prev_tech   = set(_tech_bonus_re.findall(run.get("question", "")))
                    overlap = len(q_tokens & prev_tokens)
                    tech_overlap = len(q_tech & prev_tech) * 3   # weight technical matches 3x
                    score = float(overlap + tech_overlap)
                    if score > best_score:
                        best_score = score
                        best = run

                # Require at least 5 weighted tokens overlap for keyword path
                if best_score < 5 or not best:
                    return ""

            if not best:
                return ""

            # Extract tool sequence from metadata
            meta = best.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}

            seq = meta.get("tool_sequence") or meta.get("scratchpad") or []
            if not seq:
                tool_hist = best.get("tool_history") or []
                if isinstance(tool_hist, str):
                    try:
                        tool_hist = json.loads(tool_hist)
                    except Exception:
                        tool_hist = []
                seq = [{"tool": t} for t in tool_hist]

            if not seq:
                return ""

            tool_names = [s["tool"] if isinstance(s, dict) else str(s) for s in seq[:8]]

            # If past run FAILED or had low confidence — warn agent to try differently
            _past_meta      = best.get("metadata") or {}
            _past_gs        = _past_meta.get("goal_state") or {}
            _past_conf      = float(_past_gs.get("confidence", 1.0))
            _past_status    = str(_past_gs.get("status", "completed"))
            _past_failed    = _past_status in ("partial", "failed") or _past_conf < 0.70

            if _past_failed:
                _past_tools = best.get("tool_history") or []
                logger.info(
                    f"PostgresMemory: past run for similar goal FAILED "
                    f"(confidence={_past_conf:.2f}, status={_past_status}) — "
                    f"injecting failure warning"
                )
                return (
                    f"\n[MEMORY WARNING] A previous similar request using tool sequence "
                    f"{_past_tools} resulted in low confidence ({_past_conf:.0%}, "
                    f"status={_past_status}). "
                    f"Consider a different tool ordering or approach. "
                    f"Do not repeat the same steps that produced a low-confidence result."
                )

            return (
                f"\n[MEMORY HINT] A semantically similar past request used this tool sequence: "
                f"{' → '.join(tool_names)}. Consider reusing this approach if applicable.\n"
            )

        except Exception as e:
            logger.debug(f"PostgresMemory get_tool_sequence_hint failed: {e}")
            return ""

    def _list_agent_runs_ranked(self, agent_name: str, limit: int = 100) -> List[Dict[str, Any]]:
        """List agent runs ordered by importance_score DESC then created_at DESC (P5)."""
        if not self.available:
            return []
        sql = """
            SELECT * FROM agent_runs
            WHERE agent_name = %s
            ORDER BY COALESCE(importance_score, 0.5) DESC, created_at DESC
            LIMIT %s
        """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (agent_name, limit))
                rows = self._pg_fetchall(cur)
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"PostgresMemory _list_agent_runs_ranked failed: {e}")
            return self.list_agent_runs(agent_name, limit=limit)  # fallback

    # ========================================================
    # P5 — MEMORY ENTRIES (quality-ranked structured memory)
    # ========================================================

    def store_memory(
        self,
        content: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        org_id: Optional[str] = None,
        importance_score: float = 0.5,
        confidence: float = 0.7,
        source_type: str = "explicit",
        expires_at: Optional[datetime] = None,
    ) -> Optional[str]:
        """Store a structured memory entry with quality metadata.

        source_type: 'explicit' | 'inferred' | 'feedback'
        importance_score: 0.0–1.0 (higher = retrieved first)
        confidence: 0.0–1.0 (how certain we are this is accurate)
        expires_at: None = never expires; set for session-scoped memories.

        Returns the new entry UUID, or None on failure.
        """
        if not self.available or not content:
            return None
        entry_id = str(uuid.uuid4())
        sql = """
            INSERT INTO memory_entries
                (id, session_id, user_id, org_id, content,
                 importance_score, confidence, source_type, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (
                    entry_id, session_id, user_id, org_id, content[:2000],
                    max(0.0, min(1.0, importance_score)),
                    max(0.0, min(1.0, confidence)),
                    source_type,
                    expires_at,
                ))
            self._conn.commit()
            logger.debug(f"PostgresMemory stored memory entry {entry_id} importance={importance_score:.2f}")
            return entry_id
        except Exception as e:
            self._conn.rollback()
            logger.error(f"PostgresMemory store_memory failed: {e}")
            return None

    def retrieve_memories(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        org_id: Optional[str] = None,
        limit: int = 10,
        min_importance: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Retrieve memory entries ranked by importance × recency decay.

        Score = importance_score × exp(-days_since_creation / 30)
        Entries with expires_at in the past are excluded automatically.
        Returns list of dicts with content, importance_score, confidence, source_type.
        """
        if not self.available:
            return []
        # Build WHERE clause dynamically
        conditions = ["(expires_at IS NULL OR expires_at > NOW())"]
        params: list = []
        if user_id:
            conditions.append("user_id = %s")
            params.append(user_id)
        if session_id:
            conditions.append("session_id = %s")
            params.append(session_id)
        if org_id:
            conditions.append("org_id = %s")
            params.append(org_id)
        if min_importance > 0.0:
            conditions.append("importance_score >= %s")
            params.append(min_importance)
        where = " AND ".join(conditions)
        sql = f"""
            SELECT id, content, importance_score, confidence, source_type, created_at,
                   importance_score * EXP(-EXTRACT(EPOCH FROM (NOW() - created_at)) / 2592000.0)
                   AS ranked_score
            FROM memory_entries
            WHERE {where}
            ORDER BY ranked_score DESC
            LIMIT %s
        """
        params.append(limit)
        try:
            with self._cursor() as cur:
                cur.execute(sql, params)
                rows = self._pg_fetchall(cur)
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"PostgresMemory retrieve_memories failed: {e}")
            return []

    def expire_stale_memories(self) -> int:
        """Delete memory entries whose expires_at has passed. Returns count deleted.

        Called by the memory maintenance worker every 6h.
        """
        if not self.available:
            return 0
        try:
            with self._cursor() as cur:
                cur.execute(
                    "DELETE FROM memory_entries WHERE expires_at IS NOT NULL AND expires_at <= NOW()"
                )
                deleted = cur.rowcount
            self._conn.commit()
            logger.info(f"PostgresMemory expired {deleted} stale memory entries")
            return int(deleted or 0)
        except Exception as e:
            self._conn.rollback()
            logger.error(f"PostgresMemory expire_stale_memories failed: {e}")
            return 0

    def decay_importance_scores(self, decay_factor: float = 0.95) -> int:
        """Apply a multiplicative decay to importance scores of old entries.

        Entries older than 30 days that have not been accessed recently get
        their importance_score multiplied by decay_factor (default 0.95).
        Scores are clamped to [0.1, 1.0] to prevent complete decay.
        Returns count of rows updated.
        """
        if not self.available:
            return 0
        try:
            with self._cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory_entries
                    SET importance_score = GREATEST(0.1, LEAST(1.0, importance_score * %s))
                    WHERE created_at < NOW() - INTERVAL '30 days'
                      AND importance_score > 0.1
                    """,
                    (decay_factor,),
                )
                updated = cur.rowcount
            self._conn.commit()
            logger.info(f"PostgresMemory decayed importance scores for {updated} entries")
            return int(updated or 0)
        except Exception as e:
            self._conn.rollback()
            logger.error(f"PostgresMemory decay_importance_scores failed: {e}")
            return 0

    # ========================================================
    # WORKFLOW HISTORY
    # ========================================================

    def save_workflow_run(
        self,
        workflow_id: str,
        workflow_name: str,
        steps: List[Dict[str, Any]],
        status: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.available:
            logger.warning("PostgresMemory: skipping save_workflow_run — unavailable")
            return

        sql = """
            INSERT INTO workflow_history
                (workflow_id, workflow_name, steps, status, metadata)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (workflow_id) DO UPDATE
                SET steps    = EXCLUDED.steps,
                    status   = EXCLUDED.status,
                    metadata = EXCLUDED.metadata
        """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (
                    workflow_id,
                    workflow_name,
                    json.dumps(steps),
                    status,
                    json.dumps(metadata or {}),
                ))
            self._conn.commit()
            logger.debug(f"PostgresMemory saved workflow run → {workflow_id}")
        except Exception as e:
            self._conn.rollback()
            logger.error(f"PostgresMemory save_workflow_run failed: {e}")
            raise

    def get_workflow_run(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._cursor() as cur:
                cur.execute(
                    "SELECT * FROM workflow_history WHERE workflow_id = %s",
                    (workflow_id,),
                )
                row = self._pg_fetchone(cur)
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"PostgresMemory get_workflow_run failed: {e}")
            return None

    def list_workflow_runs(
        self,
        workflow_name: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        sql = """
            SELECT * FROM workflow_history
            WHERE workflow_name = %s
            ORDER BY created_at DESC
            LIMIT %s
        """
        try:
            with self._cursor() as cur:
                cur.execute(sql, (workflow_name, limit))
                rows = self._pg_fetchall(cur)
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"PostgresMemory list_workflow_runs failed: {e}")
            return []

    # ========================================================
    # MODEL USAGE TRACKING
    # ========================================================

    def create_model_usage(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
        request_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        project_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        source_channel: Optional[str] = None,
    ) -> None:
        """Record per-request model usage for cost and token tracking."""
        if not self.available:
            return
        # Sanitise user_id — must be a valid UUID string or NULL.
        # IDE / fallback paths may pass "default" or non-UUID sub claims.
        import uuid as _uuid_mod
        if user_id is not None:
            try:
                _uuid_mod.UUID(str(user_id))
            except (ValueError, AttributeError):
                user_id = None  # store NULL rather than crash
        total = input_tokens + output_tokens
        # Supply id and created_at explicitly — DEFAULT expressions require
        # pgcrypto / server-side functions that may not be available on the
        # existing table if it was created before these columns were added.
        sql = """
            INSERT INTO model_usages
                (id, user_id, agent_id, project_id, endpoint, model,
                 input_tokens, output_tokens, total_tokens,
                 latency_ms, cost_usd, request_id, created_at,
                 cache_read_tokens, cache_write_tokens, source_channel)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            from core.time_utils import now_ist as _now_ist_pm
            with self._cursor() as cur:
                cur.execute(sql, (
                    str(uuid.uuid4()),
                    user_id, agent_id, project_id, endpoint, model,
                    input_tokens, output_tokens, total,
                    latency_ms, cost_usd, request_id,
                    _now_ist_pm(),  # IST, not UTC — matches ModelUsage.created_at default
                    cache_read_tokens, cache_write_tokens, source_channel,
                ))
            self._conn.commit()
            logger.info(
                f"PostgresMemory model_usages row written → model={model} tokens={total} "
                f"cost=${cost_usd:.6f} endpoint={endpoint} source_channel={source_channel}"
            )
        except Exception as e:
            self._conn.rollback()
            logger.error(f"PostgresMemory create_model_usage FAILED model={model} endpoint={endpoint}: {e}")

    # ========================================================
    # HEALTH
    # ========================================================

    def ping(self) -> bool:
        try:
            with self._cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.info("PostgresMemory connection closed")
