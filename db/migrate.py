#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DATABASE MIGRATION — safe CREATE TABLE IF NOT EXISTS runner
# Run at any time; idempotent.
# Usage: python db/migrate.py
# ============================================================

import sys
import os
import re as _re_mod

def _cfg(key: str, default: str = "") -> str:
    """Read a configuration value from the environment.
    Generic accessor used for all connection parameters including credentials.
    """
    return os.environ.get(key, default)

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before database.py reads os.getenv()
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    pass  # dotenv not installed — fall back to shell env vars

# CKMS — decrypt POSTGRES_PASSWORD / POSTGRES_MIGRATE_PASSWORD before db.database
# is imported. Fail-fast on any HSM / DB / crypto error.
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

from db.database import Base, DB_SCHEMA

# ── Migration engine ──────────────────────────────────────────────────────────
# Uses POSTGRES_MIGRATE_USER if set (superuser / ainxt_migrate); otherwise falls
# back to POSTGRES_USER.  Migrations need SUPERUSER locally for CREATE EXTENSION.
# Prod: POSTGRES_MIGRATE_USER=ainxt_migrate, POSTGRES_MIGRATE_PASSWORD=<pw>.
# Local: POSTGRES_MIGRATE_USER=admin (Mac Homebrew superuser), password blank.
_MIG_USER = os.getenv("POSTGRES_MIGRATE_USER") or os.getenv("POSTGRES_USER", "postgres")
# No hardcoded localhost default — reuse core.config.POSTGRES_HOST (itself
# no-default) so this script and the app agree on what "unset" means.
from core.config import POSTGRES_DB as _CONFIG_POSTGRES_DB, POSTGRES_HOST as _CONFIG_POSTGRES_HOST
_MIG_HOST = os.getenv("POSTGRES_HOST", _CONFIG_POSTGRES_HOST)
_MIG_PORT = os.getenv("POSTGRES_PORT", "5432")
_MIG_DB   = os.getenv("POSTGRES_DB", _CONFIG_POSTGRES_DB)

_MIG_CONNECT_ARGS = {
    "connect_timeout": 10,
    "options": f"-c statement_timeout=120000 -c search_path={DB_SCHEMA},public",
}

from sqlalchemy import create_engine as _create_engine
from sqlalchemy.engine import URL as _URL
engine = _create_engine(
    _URL.create(
        drivername="postgresql+psycopg2",
        username=_MIG_USER,
        password=_cfg("POSTGRES_MIGRATE_PASSWORD") or _cfg("POSTGRES_PASSWORD"),
        host=_MIG_HOST,
        port=int(_MIG_PORT),
        database=_MIG_DB,
        query={"options": f"-csearch_path={DB_SCHEMA},public"},
    ),
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=0,
    pool_timeout=30,
    connect_args=_MIG_CONNECT_ARGS,
    echo=False,
)

# ── Vector migration engine (PGS02) ──────────────────────────────────────────
# Prod: separate host for pgvector. Local: ainxt_vector DB (simulates the split).
# DDL for document_embeddings MUST run here, not on engine (PGS01).
_VEC_MIG_USER = os.getenv("PGVECTOR_USER") or _MIG_USER
_VEC_MIG_HOST = os.getenv("PGVECTOR_HOST", _MIG_HOST)
_VEC_MIG_PORT = os.getenv("PGVECTOR_PORT", _MIG_PORT)
_VEC_MIG_DB   = os.getenv("PGVECTOR_DB", _MIG_DB)

vector_engine = _create_engine(
    _URL.create(
        drivername="postgresql+psycopg2",
        username=_VEC_MIG_USER,
        password=_cfg("PGVECTOR_PASSWORD") or _cfg("POSTGRES_MIGRATE_PASSWORD") or _cfg("POSTGRES_PASSWORD"),
        host=_VEC_MIG_HOST,
        port=int(_VEC_MIG_PORT),
        database=_VEC_MIG_DB,
        query={"options": f"-csearch_path={DB_SCHEMA},public"},
    ),
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=0,
    pool_timeout=30,
    connect_args=_MIG_CONNECT_ARGS,
    echo=False,
)

# Import all models so SQLAlchemy metadata is populated
import db.models  # noqa: F401


def _run_vector_ddl():
    """
    Create and migrate document_embeddings on PGS02 (vector_engine).
    In prod PGS02 is a separate host — this DDL must NEVER run on PGS01 (engine).
    Idempotent: uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS throughout.
    """
    from sqlalchemy import text as _text
    _VECTOR_DDL = f"""
-- Extensions go to public schema (extension objects are cluster-wide)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Ensure application schema exists on PGS02
CREATE SCHEMA IF NOT EXISTS {DB_SCHEMA};

SET search_path = {DB_SCHEMA}, public;

CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.document_embeddings (
    id             VARCHAR(36)  PRIMARY KEY,
    repo           VARCHAR(255) NOT NULL,
    file_path      TEXT,
    chunk_index    INTEGER      NOT NULL DEFAULT 0,
    content        TEXT         NOT NULL,
    embedding      vector(768),
    metadata       JSONB        NOT NULL DEFAULT '{{}}',
    content_hash   VARCHAR(64),
    line_start     INTEGER,
    line_end       INTEGER,
    classification VARCHAR(50)  NOT NULL DEFAULT 'INTERNAL',
    owner_team     VARCHAR(255),
    org_id         VARCHAR(255),
    uploaded_by    VARCHAR(255),
    allowed_roles  JSONB        NOT NULL DEFAULT '[]',
    allowed_users  JSONB        NOT NULL DEFAULT '[]',
    product_id     UUID,
    department     VARCHAR(255),
    branch         VARCHAR(255),
    created_at     TIMESTAMP    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_doc_embed_chunk UNIQUE (repo, file_path, chunk_index)
);

-- HNSW index for fast ANN search
CREATE INDEX IF NOT EXISTS idx_doc_embed_hnsw
    ON {DB_SCHEMA}.document_embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_doc_embed_repo          ON {DB_SCHEMA}.document_embeddings (repo);
CREATE INDEX IF NOT EXISTS idx_doc_embed_classification ON {DB_SCHEMA}.document_embeddings (classification);
CREATE INDEX IF NOT EXISTS idx_doc_embed_owner_team    ON {DB_SCHEMA}.document_embeddings (owner_team);
CREATE INDEX IF NOT EXISTS idx_doc_embed_org_id        ON {DB_SCHEMA}.document_embeddings (org_id);
CREATE INDEX IF NOT EXISTS idx_doc_embed_product_id    ON {DB_SCHEMA}.document_embeddings (product_id) WHERE product_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_doc_embed_department    ON {DB_SCHEMA}.document_embeddings (department) WHERE department IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_doc_embed_branch        ON {DB_SCHEMA}.document_embeddings (branch) WHERE branch IS NOT NULL;

-- Dedup is enforced at upload time on knowledge_docs.content_hash; chunk-level
-- uniqueness would collide on shared boilerplate. The column stays nullable for
-- code-repo / Confluence / Jira incremental indexers (use idx_doc_embed_repo_hash).
DROP INDEX IF EXISTS {DB_SCHEMA}.uq_doc_embed_content_hash;

-- Idempotent column additions for existing installs
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS product_id   UUID;
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS department   VARCHAR(255);
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS line_start   INTEGER;
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS line_end     INTEGER;
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS classification VARCHAR(50) NOT NULL DEFAULT 'INTERNAL';
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS owner_team   VARCHAR(255);
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS org_id       VARCHAR(255);
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS uploaded_by  VARCHAR(255);
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS allowed_roles JSONB NOT NULL DEFAULT '[]';
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS allowed_users JSONB NOT NULL DEFAULT '[]';
ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS branch        VARCHAR(255);
CREATE INDEX IF NOT EXISTS idx_doc_embed_branch ON {DB_SCHEMA}.document_embeddings (branch) WHERE branch IS NOT NULL;
"""
    try:
        with vector_engine.connect() as conn:
            conn.execute(_text(_VECTOR_DDL))
            conn.commit()
        print("  ok PGS02 vector DDL: document_embeddings table + HNSW index + columns")
    except Exception as exc:
        print(f"  ! PGS02 vector DDL error: {exc}")


def run_migrations():
    print("Running migrations (CREATE TABLE IF NOT EXISTS)...")

    # ── Step 0: Create ainxt schema on both PGS01 and PGS02 ──────────────────
    # Must happen before any CREATE TABLE or CREATE EXTENSION that references
    # the ainxt schema.  Idempotent — safe to run on an existing database.
    from sqlalchemy import text as _text
    for _eng, _db_label in [(engine, "PGS01"), (vector_engine, "PGS02")]:
        try:
            _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
            with _eng.connect() as _conn:
                _conn.execute(_text(
                    f"CREATE SCHEMA IF NOT EXISTS {DB_SCHEMA} "
                    f"AUTHORIZATION CURRENT_USER"
                ))
                # Grant app user access to the schema (idempotent)
                _conn.execute(_text(
                    f"GRANT USAGE ON SCHEMA {DB_SCHEMA} TO {_app_user}"
                ))
                _conn.execute(_text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {DB_SCHEMA} TO {_app_user}"
                ))
                _conn.execute(_text(
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {DB_SCHEMA} TO {_app_user}"
                ))
                _conn.execute(_text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {DB_SCHEMA} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_app_user}"
                ))
                _conn.execute(_text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {DB_SCHEMA} GRANT USAGE, SELECT ON SEQUENCES TO {_app_user}"
                ))
                _conn.commit()
            print(f"  ok {_db_label} schema '{DB_SCHEMA}' ready (granted to {_app_user})")
        except Exception as _schema_err:
            print(f"  ! {_db_label} schema creation warning: {_schema_err}")

    # ── Step 0b: PGS02 — document_embeddings (vector DB, separate from PGS01) ─
    _run_vector_ddl()

    # ── Step 1: Enable Postgres extensions on PGS01 ──────────────────────────
    try:
        with engine.connect() as _conn:
            _conn.execute(_text("CREATE EXTENSION IF NOT EXISTS vector"))
            _conn.execute(_text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            _conn.commit()
        print("  ok PGS01 extensions enabled: vector, pgcrypto")
    except Exception as _ext_err:
        print(f"  ! PGS01 extension setup failed: {_ext_err}")
        # Patch the ORM so create_all() uses Text instead of VECTOR
        try:
            import db.models as _m
            _m._PGVECTOR_AVAILABLE = False
            col = _m.DocumentEmbedding.__table__.c.get("embedding")
            if col is not None:
                from sqlalchemy import Text as _Text
                col.type = _Text()
        except Exception:
            pass

    # ── Step 2: Create all ORM-defined tables on PGS01 ──────────────────────
    # Exclude document_embeddings — it lives on PGS02 (vector_engine), not PGS01.
    # Exclude workspace_messages — HASH-partitioned table managed by Part S24 raw DDL;
    # SQLAlchemy create_all() cannot create partitioned tables with composite PKs.
    try:
        _pgs01_tables = [
            t for name, t in Base.metadata.tables.items()
            if not name.endswith("document_embeddings")
            and not name.endswith("workspace_messages")
        ]
        Base.metadata.create_all(bind=engine, tables=_pgs01_tables)
        print("PGS01 tables created or already exist:")
        for t in _pgs01_tables:
            print(f"  ok {t.name}")
    except Exception as e:
        print(f"Migration failed: {e}")
        sys.exit(1)

    # ── Step 3: Extra raw DDL (ALTERs, indexes, vault, etc.) ────────────────
    # The ORM model in db/models.py already covers most tables via
    # create_all() above.  The block below is a safety net for
    # environments where the raw psycopg2 vault store connects to
    # a database that was not bootstrapped through SQLAlchemy.
    _run_raw_ddl()

    # Surface anything that failed and fail the process if the schema is not
    # actually usable — see _report_and_exit().
    _report_and_exit()


def _run_raw_ddl():
    """
    Execute raw DDL statements not covered by SQLAlchemy models.
    Split into two parts so pgvector failures don't block essential ALTERs.
    """
    # model_rate_table.id needs its server-side default before ANY part seeds
    # the model catalogue: Parts L, T1 and T1b all INSERT without naming id.
    _part_oss3_model_rate_table_id_default()
    # Generic: every UUID primary key needs a database-side default before any
    # raw-SQL part seeds a table without naming `id`.
    _part_oss6_uuid_pk_server_defaults()
    _part_oss7_mirror_orm_defaults()
    # Must precede Part G, which adds fk_chat_attachments_chat_id.
    _part_oss5_chat_attachments_chat_id_uuid()

    # ── Part A: Always-required DDL ──────────────────────────────────────────
    _MAIN_DDL = f"""
SET search_path = {DB_SCHEMA}, public;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS credential_vault (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT UNIQUE NOT NULL,
    encrypted_value  TEXT NOT NULL,
    description      TEXT DEFAULT '',
    owner            TEXT DEFAULT 'system',
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- org_tree: direct_reports changed from INTEGER to TEXT (stores DN/name list)
ALTER TABLE org_tree ALTER COLUMN direct_reports TYPE TEXT USING direct_reports::TEXT;


-- Phase 15: Multi-tenant org_id columns
ALTER TABLE agents_pg    ADD COLUMN IF NOT EXISTS org_id VARCHAR(255) NOT NULL DEFAULT 'default';
ALTER TABLE skills_pg    ADD COLUMN IF NOT EXISTS org_id VARCHAR(255) NOT NULL DEFAULT 'default';
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS org_id VARCHAR(255) NOT NULL DEFAULT 'default';
ALTER TABLE mcp_servers  ADD COLUMN IF NOT EXISTS org_id VARCHAR(255) NOT NULL DEFAULT 'default';

-- KB dept ACL: uploader's department stored at upload time (used for Approval Inbox scoping)
ALTER TABLE knowledge_docs ADD COLUMN IF NOT EXISTS uploaded_by_dept VARCHAR(255);
-- document_embeddings dept ACL column (used for SQL-level RAG filtering)
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS department VARCHAR(255);

-- model_usages.product_id — required for chargeback tracking via Kafka consumer
ALTER TABLE model_usages ADD COLUMN IF NOT EXISTS product_id UUID;

-- projects_pg.updated_at — required for project last-modified tracking
ALTER TABLE projects_pg ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();

-- Phase 17: Agent stage column
ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS stage VARCHAR(50) NOT NULL DEFAULT 'production';

-- ABStudio governance layer: template-instance provenance + approval hashing.
-- source_template_hash records the template content an artifact was created
-- from; last_approved_hash records the content that was last approved. When an
-- artifact's canonical hash differs from both, it needs (re)approval.
ALTER TABLE agents_pg    ADD COLUMN IF NOT EXISTS source_template_id   VARCHAR(255);
ALTER TABLE agents_pg    ADD COLUMN IF NOT EXISTS source_template_hash VARCHAR(64);
ALTER TABLE agents_pg    ADD COLUMN IF NOT EXISTS last_approved_hash   VARCHAR(64);
ALTER TABLE skills_pg    ADD COLUMN IF NOT EXISTS source_template_id   VARCHAR(255);
ALTER TABLE skills_pg    ADD COLUMN IF NOT EXISTS source_template_hash VARCHAR(64);
ALTER TABLE skills_pg    ADD COLUMN IF NOT EXISTS last_approved_hash   VARCHAR(64);
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS source_template_id   VARCHAR(255);
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS source_template_hash VARCHAR(64);
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS last_approved_hash   VARCHAR(64);

-- Phase 17: Agent episodic memory table
CREATE TABLE IF NOT EXISTS agent_memory (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name VARCHAR(255) NOT NULL,
    key        VARCHAR(500) NOT NULL,
    value      TEXT,
    tags       JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(agent_name, key)
);

-- Phase 18: Audit signature column on events
ALTER TABLE sdlc_run_events ADD COLUMN IF NOT EXISTS signature TEXT;

-- Multimodal Chat: attachments table
CREATE TABLE IF NOT EXISTS chat_attachments (
    id           VARCHAR(36) PRIMARY KEY,
    chat_id      VARCHAR(36) NOT NULL,
    file_name    VARCHAR(512) NOT NULL,
    file_type    VARCHAR(50) NOT NULL,
    file_size    INTEGER DEFAULT 0,
    storage_path TEXT NOT NULL,
    parsed_text  TEXT,
    created_by   VARCHAR(255),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ca_chat ON chat_attachments(chat_id);

-- Server-side uploaded-asset persistence: owner ACL + asset kind.
-- Additive/backward-compatible; legacy rows keep NULL and fall back to created_by.
ALTER TABLE chat_attachments ADD COLUMN IF NOT EXISTS user_id VARCHAR(255);
ALTER TABLE chat_attachments ADD COLUMN IF NOT EXISTS kind    VARCHAR(16) DEFAULT 'document';
-- Persistent image context (multi-turn image memory): the full Gemini Vision
-- description and a short caption. Populated for image uploads so later turns
-- can replay a compact caption into history without re-sending image bytes.
ALTER TABLE chat_attachments ADD COLUMN IF NOT EXISTS image_description TEXT;
ALTER TABLE chat_attachments ADD COLUMN IF NOT EXISTS image_caption     VARCHAR(600);
CREATE INDEX IF NOT EXISTS idx_ca_user ON chat_attachments(user_id);
UPDATE chat_attachments SET user_id = created_by WHERE user_id IS NULL AND created_by IS NOT NULL;

-- Multimodal Chat: extra columns on chat_messages
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS selected_model VARCHAR(255);
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS attachment_ids JSONB DEFAULT '[]';
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS token_usage INTEGER DEFAULT 0;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS cost FLOAT DEFAULT 0.0;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS language VARCHAR(20);

-- Governance columns on agents_pg
ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS status VARCHAR(50) NOT NULL DEFAULT 'PRODUCTION';
ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS created_by VARCHAR(255);
ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS approved_by VARCHAR(255);
ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS is_production BOOLEAN DEFAULT TRUE;

-- Governance columns on skills_pg
ALTER TABLE skills_pg ADD COLUMN IF NOT EXISTS status VARCHAR(50) NOT NULL DEFAULT 'PRODUCTION';
ALTER TABLE skills_pg ADD COLUMN IF NOT EXISTS created_by VARCHAR(255);
ALTER TABLE skills_pg ADD COLUMN IF NOT EXISTS approved_by VARCHAR(255);
ALTER TABLE skills_pg ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE skills_pg ADD COLUMN IF NOT EXISTS is_production BOOLEAN DEFAULT TRUE;

-- Governance columns on mcp_servers
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS status VARCHAR(50) NOT NULL DEFAULT 'PRODUCTION';
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS created_by VARCHAR(255);
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS approved_by VARCHAR(255);
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS is_production BOOLEAN DEFAULT TRUE;

-- Phase 19: Governance columns on workflows_pg
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS status VARCHAR(50) NOT NULL DEFAULT 'PRODUCTION';
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS created_by VARCHAR(255);
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS approved_by VARCHAR(255);
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS is_production BOOLEAN DEFAULT TRUE;

-- Project persistence: project_id FK on workflows, chats
ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS project_id VARCHAR(255);
ALTER TABLE chats ADD COLUMN IF NOT EXISTS project_id  VARCHAR(255);
ALTER TABLE chats ADD COLUMN IF NOT EXISTS session_id  VARCHAR(255);
ALTER TABLE chats ADD COLUMN IF NOT EXISTS agent_id    VARCHAR(255);
CREATE INDEX IF NOT EXISTS idx_workflows_project ON workflows_pg(project_id);
CREATE INDEX IF NOT EXISTS idx_chats_project     ON chats(project_id);
CREATE INDEX IF NOT EXISTS idx_chats_session     ON chats(session_id);

-- Budget controls on projects
ALTER TABLE projects_pg ADD COLUMN IF NOT EXISTS budget_limit_usd FLOAT;
ALTER TABLE projects_pg ADD COLUMN IF NOT EXISTS budget_used_usd FLOAT NOT NULL DEFAULT 0.0;

-- Threads & messages (Postgres-native, replaces Redis db=3)
CREATE TABLE IF NOT EXISTS threads_pg (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       VARCHAR(500) NOT NULL,
    description TEXT DEFAULT '',
    project_id  VARCHAR(255) DEFAULT '',
    repo        VARCHAR(255) DEFAULT '',
    created_by  VARCHAR(255) DEFAULT 'user',
    labels      JSONB NOT NULL DEFAULT '[]',
    priority    VARCHAR(50) NOT NULL DEFAULT 'Medium',
    status      VARCHAR(50) NOT NULL DEFAULT 'open',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads_pg(status);
CREATE INDEX IF NOT EXISTS idx_threads_project ON threads_pg(project_id);

CREATE TABLE IF NOT EXISTS thread_messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id   UUID NOT NULL REFERENCES threads_pg(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    author      VARCHAR(255) NOT NULL DEFAULT 'user',
    mentions    JSONB NOT NULL DEFAULT '[]',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_thread_messages_thread ON thread_messages(thread_id);

-- Stats columns on thread_messages (for MessageMeta display)
ALTER TABLE thread_messages ADD COLUMN IF NOT EXISTS model_used  VARCHAR(100);
ALTER TABLE thread_messages ADD COLUMN IF NOT EXISTS tokens_in   INTEGER;
ALTER TABLE thread_messages ADD COLUMN IF NOT EXISTS tokens_out  INTEGER;
ALTER TABLE thread_messages ADD COLUMN IF NOT EXISTS cost_usd    FLOAT;
ALTER TABLE thread_messages ADD COLUMN IF NOT EXISTS latency_ms  FLOAT;

-- Inbox items (Postgres-native, replaces Redis db=3)
CREATE TABLE IF NOT EXISTS inbox_items (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     VARCHAR(255) NOT NULL,
    type        VARCHAR(100) NOT NULL DEFAULT 'notification',
    title       VARCHAR(500) NOT NULL,
    body        TEXT DEFAULT '',
    source_id   VARCHAR(255) DEFAULT '',
    metadata    JSONB NOT NULL DEFAULT '{{}}',
    read        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_inbox_user ON inbox_items(user_id);
CREATE INDEX IF NOT EXISTS idx_inbox_unread ON inbox_items(user_id, read);

-- Idempotent column additions for inbox_items (older deployments may be missing these)
ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS user_id    VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS type       VARCHAR(100) NOT NULL DEFAULT 'notification';
ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS title      VARCHAR(500) NOT NULL DEFAULT '';
ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS body       TEXT         DEFAULT '';
ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS source_id  VARCHAR(255) DEFAULT '';
ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS metadata   JSONB        NOT NULL DEFAULT '{{}}';
ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS read       BOOLEAN      NOT NULL DEFAULT FALSE;
ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW();

-- Phase 19: Durable governance audit log
CREATE TABLE IF NOT EXISTS governance_events (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type VARCHAR(50) NOT NULL,
    name        VARCHAR(255) NOT NULL,
    action      VARCHAR(50) NOT NULL,
    from_status VARCHAR(50),
    to_status   VARCHAR(50) NOT NULL,
    actor       VARCHAR(255) NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_gov_events_entity ON governance_events(entity_type, name);
CREATE INDEX IF NOT EXISTS idx_gov_events_actor  ON governance_events(actor);

-- Phase 21: Repo index status registry
CREATE TABLE IF NOT EXISTS repo_index_status (
    repo_name       TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'pending',
    triggered_by    TEXT DEFAULT 'system',
    total_chunks    INTEGER DEFAULT 0,
    indexed_chunks  INTEGER DEFAULT 0,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error_msg       TEXT
);
CREATE INDEX IF NOT EXISTS idx_repo_index_status ON repo_index_status(status);

-- content_hash retained nullable for code-repo / Confluence / Jira incremental
-- indexers. KB upload-time dedup uses knowledge_docs.content_hash.
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS file_path     TEXT;
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS line_start    INTEGER;
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS line_end      INTEGER;
-- Ensure created_at has a default so bulk inserts without explicit value work
ALTER TABLE document_embeddings ALTER COLUMN created_at SET DEFAULT NOW();
-- Idempotent drop of the now-retired chunk-level dedup unique index.
DROP INDEX IF EXISTS idx_doc_embed_hash;

-- Human feedback / RL signal on chat messages
CREATE TABLE IF NOT EXISTS message_feedback (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id VARCHAR(255) NOT NULL,
    user_id    VARCHAR(255) NOT NULL,
    rating     INTEGER      NOT NULL,  -- +1 thumbs-up, -1 thumbs-down
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_msgfb_message ON message_feedback(message_id);
CREATE INDEX IF NOT EXISTS idx_msgfb_user    ON message_feedback(user_id);

-- KB approval deferred-embed: store raw chunks at upload time, embed only on approval
ALTER TABLE knowledge_docs ADD COLUMN IF NOT EXISTS chunks JSONB;
"""

    # ── Part B: pgvector DDL (optional — requires postgresql-pgvector package) ─
    _PGVECTOR_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE document_embeddings
    ALTER COLUMN embedding TYPE vector(768)
    USING embedding::text::vector;

CREATE INDEX IF NOT EXISTS idx_doc_embed_repo ON document_embeddings(repo);
CREATE INDEX IF NOT EXISTS idx_doc_embed_vec
    ON document_embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""

    _engine = engine  # migrate engine (POSTGRES_MIGRATE_USER — has DDL privileges)
    from sqlalchemy import text as _text

    # Always run Part A
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_MAIN_DDL))
            conn.commit()
        print("  ok Phase 15-19 DDL: multimodal + governance + credential_vault (raw DDL)")
    except Exception as exc:
        print(f"  ! Main raw DDL error: {exc}")

    # Optionally run Part B
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_PGVECTOR_DDL))
            conn.commit()
        print("  ok pgvector extension + HNSW index applied")
    except Exception as exc:
        print(f"  ! pgvector DDL skipped (install postgresql-pgvector to enable): {exc}")

    # ── Part C: Data fix — add repo_ prefix to bare repo names ──────────────
    # index_worker.py used to store bare repo names (e.g. 'upi-stats').
    # The index_router.py queries with WHERE repo LIKE 'repo_%', so existing
    # rows were invisible.  This one-time UPDATE renames them.
    _DATA_FIX = """
UPDATE document_embeddings
SET repo = 'repo_' || repo
WHERE repo NOT LIKE 'repo_%'
  AND repo NOT LIKE 'docs_kb:%';
"""
    try:
        with _engine.connect() as conn:
            result = conn.execute(_text(_DATA_FIX))
            conn.commit()
        print(f"  ok Data fix: repo prefix migration applied ({result.rowcount} rows updated)")
    except Exception as exc:
        print(f"  ! Data fix (repo prefix) failed: {exc}")

    # ── Part D: RAG ACL, new tables, composite indexes ───────────────────────
    _PART_D_DDL = """
-- RAG Access Control columns on document_embeddings
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS classification  VARCHAR(50)  NOT NULL DEFAULT 'INTERNAL';
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS owner_team      VARCHAR(255);
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS org_id          VARCHAR(255);
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS uploaded_by     VARCHAR(255);
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS allowed_roles   JSONB        NOT NULL DEFAULT '[]';
ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS allowed_users   JSONB        NOT NULL DEFAULT '[]';
-- Ensure defaults on columns that may have been added without them in earlier migrations
ALTER TABLE document_embeddings ALTER COLUMN allowed_roles   SET DEFAULT '[]';
ALTER TABLE document_embeddings ALTER COLUMN allowed_users   SET DEFAULT '[]';
ALTER TABLE document_embeddings ALTER COLUMN classification  SET DEFAULT 'general';

CREATE INDEX IF NOT EXISTS idx_doc_embed_classification ON document_embeddings(classification);
CREATE INDEX IF NOT EXISTS idx_doc_embed_owner_team     ON document_embeddings(owner_team);
CREATE INDEX IF NOT EXISTS idx_doc_embed_org_id         ON document_embeddings(org_id);

-- Postgres memory tables (conversations, agent_runs, workflow_history)
-- Previously created at runtime by postgres_memory.py using app user — moved here
-- so migrate user (with CREATE privilege) owns the DDL.
CREATE TABLE IF NOT EXISTS conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id, created_at);

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

-- Grant app user access.
-- Guarded on the role existing: `ainxt_app` is a deployment convention and is
-- absent on a stock install, where the application connects as POSTGRES_USER
-- (the schema owner) and already holds these rights. Unguarded, these three
-- statements aborted the whole of Part D with
-- `role "ainxt_app" does not exist`, taking the rest of the part with them.
DO $grant$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ainxt_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON conversations    TO ainxt_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON agent_runs       TO ainxt_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_history TO ainxt_app;
  END IF;
END $grant$;

-- User operational fields
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at  TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS account_status VARCHAR(50) NOT NULL DEFAULT 'active';
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN     NOT NULL DEFAULT FALSE;

-- AgentVersion unique constraint (idempotent via DO block)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_agent_version'
    ) THEN
        ALTER TABLE agent_versions ADD CONSTRAINT uq_agent_version UNIQUE (agent_id, version);
    END IF;
END$$;

-- RAG immutable audit log (PCI/DSS requirement — never delete rows)
CREATE TABLE IF NOT EXISTS rag_access_log (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        VARCHAR(255) NOT NULL,
    user_role      VARCHAR(50)  NOT NULL,
    org_id         VARCHAR(255),
    query_hash     VARCHAR(64)  NOT NULL,
    chunk_id       VARCHAR(36)  NOT NULL,
    repo           VARCHAR(255) NOT NULL,
    file_path      TEXT         NOT NULL,
    classification VARCHAR(50)  NOT NULL,
    access_granted BOOLEAN      NOT NULL,
    deny_reason    VARCHAR(255),
    session_id     VARCHAR(255),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rag_log_user       ON rag_access_log(user_id);
CREATE INDEX IF NOT EXISTS idx_rag_log_chunk      ON rag_access_log(chunk_id);
CREATE INDEX IF NOT EXISTS idx_rag_log_org        ON rag_access_log(org_id);
CREATE INDEX IF NOT EXISTS idx_rag_log_session    ON rag_access_log(session_id);
CREATE INDEX IF NOT EXISTS idx_rag_log_created    ON rag_access_log(created_at);

-- Engineer work context (cross-session continuity)
CREATE TABLE IF NOT EXISTS engineer_work_context (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          VARCHAR(255) NOT NULL UNIQUE,
    summary          TEXT,
    active_repos     JSONB        NOT NULL DEFAULT '[]',
    active_tickets   JSONB        NOT NULL DEFAULT '[]',
    recent_files     JSONB        NOT NULL DEFAULT '[]',
    recent_decisions JSONB        NOT NULL DEFAULT '[]',
    session_count    INTEGER      NOT NULL DEFAULT 0,
    last_session_at  TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_eng_ctx_user ON engineer_work_context(user_id);

-- Composite indexes for high-cardinality queries at 2000 users
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_created
    ON chat_messages(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sdlc_runs_jira_key
    ON sdlc_runs(jira_key);
CREATE INDEX IF NOT EXISTS idx_sdlc_run_events_run_created
    ON sdlc_run_events(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_model_usages_user_created
    ON model_usages(user_id, created_at);
"""
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_PART_D_DDL))
            conn.commit()
        print("  ok Part D DDL: RAG ACL columns, rag_access_log, engineer_work_context, composite indexes")
    except Exception as exc:
        print(f"  ! Part D DDL error: {exc}")

    # ── Part E: Workflow runs table (P1-11 Postgres persistence) ────────────
    _PART_E_DDL = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_id   VARCHAR(255) PRIMARY KEY,
    workflow_name VARCHAR(255) NOT NULL,
    status        VARCHAR(50)  NOT NULL DEFAULT 'running',
    state_json    JSONB,
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ended_at      TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_name   ON workflow_runs(workflow_name);
"""
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_PART_E_DDL))
            conn.commit()
        print("  ok Part E DDL: workflow_runs table")
    except Exception as exc:
        print(f"  ! Part E DDL error: {exc}")


    # ── Part F: Code symbols, repo permissions, feedback/traceability columns ──
    _PART_F_DDL = """
-- code_symbols table
CREATE TABLE IF NOT EXISTS code_symbols (
    id           BIGSERIAL PRIMARY KEY,
    repo         TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    symbol_name  TEXT NOT NULL,
    symbol_type  VARCHAR(50) NOT NULL,
    language     VARCHAR(30) NOT NULL,
    line_start   INTEGER,
    line_end     INTEGER,
    signature    TEXT,
    parent_name  TEXT,
    embedding_id TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_symbols_repo ON code_symbols(repo);
CREATE INDEX IF NOT EXISTS idx_symbols_repo_name ON code_symbols(repo, symbol_name);
CREATE INDEX IF NOT EXISTS idx_symbols_name_lower ON code_symbols(lower(symbol_name));
CREATE INDEX IF NOT EXISTS idx_symbols_type ON code_symbols(repo, symbol_type);

-- repo_permissions table
CREATE TABLE IF NOT EXISTS repo_permissions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo       TEXT NOT NULL,
    user_id    VARCHAR(255),
    user_role  VARCHAR(50),
    granted    BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_repo_perm UNIQUE (repo, user_id, user_role)
);
CREATE INDEX IF NOT EXISTS idx_repo_perm_repo ON repo_permissions(repo);
CREATE INDEX IF NOT EXISTS idx_repo_perm_user ON repo_permissions(user_id);

-- Add comment field to message_feedback if not exists
ALTER TABLE message_feedback ADD COLUMN IF NOT EXISTS comment TEXT;
-- Add retrieved_chunk_ids to chat_messages for feedback traceability
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS retrieved_chunk_ids JSONB DEFAULT '[]';
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS confidence FLOAT DEFAULT 0.0;
"""
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_PART_F_DDL))
            conn.commit()
        print("  ok Part F DDL: code_symbols, repo_permissions, feedback/traceability columns")
    except Exception as exc:
        print(f"  ! Part F DDL error: {exc}")


    # ── Part G: Strict policy — missing indexes, FK constraints, immutability, audit signing ──
    _PART_G_DDL = """
-- ─────────────────────────────────────────────────────────────
-- G1: chats(user_id) index — required for /chats/{user_id}/history
--     Without this, every history query is a full seq-scan.
-- ─────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id);

-- ─────────────────────────────────────────────────────────────
-- G2: FK constraints — orphan prevention
--     chat_attachments.chat_id has no referential integrity.
--     message_feedback.message_id has no referential integrity.
--     Both allow orphan rows when parent is deleted.
-- ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_chat_attachments_chat_id'
    ) THEN
        ALTER TABLE chat_attachments
            ADD CONSTRAINT fk_chat_attachments_chat_id
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE;
    END IF;
END$$;

-- ─────────────────────────────────────────────────────────────
-- G3: governance_events — add HMAC signature column
--     sdlc_run_events is signed (Phase 18) but governance_events
--     is not. PCI/DSS requires tamper-evident audit trails on all
--     lifecycle events, not just SDLC.
-- ─────────────────────────────────────────────────────────────
ALTER TABLE governance_events ADD COLUMN IF NOT EXISTS signature TEXT;

-- ─────────────────────────────────────────────────────────────
-- G4: rag_access_log — DB-level immutability (PCI/DSS)
--     "never delete" must be enforced at database level, not only
--     by application convention. A RULE blocks DELETE + TRUNCATE.
-- ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_rules
        WHERE tablename = 'rag_access_log' AND rulename = 'rag_access_log_no_delete'
    ) THEN
        EXECUTE 'CREATE RULE rag_access_log_no_delete AS ON DELETE TO rag_access_log DO INSTEAD NOTHING';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_rules
        WHERE tablename = 'rag_access_log' AND rulename = 'rag_access_log_no_update'
    ) THEN
        EXECUTE 'CREATE RULE rag_access_log_no_update AS ON UPDATE TO rag_access_log DO INSTEAD NOTHING';
    END IF;
END$$;

-- ─────────────────────────────────────────────────────────────
-- G5: rag_access_log composite index (user_id, created_at)
--     Audit range queries like "all accesses by user X in last 30 days"
--     need a composite index; idx_rag_log_user alone is insufficient.
-- ─────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_rag_log_user_created ON rag_access_log(user_id, created_at DESC);

-- ─────────────────────────────────────────────────────────────
-- G6: governance_events — created_at index for time-range audits
-- ─────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_gov_events_created ON governance_events(created_at DESC);

-- ─────────────────────────────────────────────────────────────
-- G7: token_usage duplicate column deprecation note
--     chat_messages has both tokens_used (ORM, gateway writes here)
--     and token_usage (stale ALTER from Phase 18, never written).
--     Rename stale column to avoid confusion. Data safe — was always 0.
-- ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chat_messages' AND column_name = 'token_usage'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chat_messages' AND column_name = 'tokens_used'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chat_messages' AND column_name = 'token_usage'
    ) AND NOT EXISTS (
        -- Part J's partitioned definition of chat_messages already declares
        -- token_usage_deprecated, so an unguarded RENAME failed with
        -- `column "token_usage_deprecated" ... already exists`.
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chat_messages' AND column_name = 'token_usage_deprecated'
    ) THEN
        ALTER TABLE chat_messages RENAME COLUMN token_usage TO token_usage_deprecated;
    END IF;
END$$;
"""
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_PART_G_DDL))
            conn.commit()
        print("  ok Part G DDL: chats user_id index, FK constraints, governance signature, rag_access_log immutability, composite indexes, duplicate column cleanup")
    except Exception as exc:
        print(f"  ! Part G DDL error: {exc}")


    # ── Part H: Security scan results table ──────────────────────────────────
    _PART_H_DDL = r"""
CREATE TABLE IF NOT EXISTS security_scan_results (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    repo            TEXT        NOT NULL,
    branch          TEXT        NOT NULL,
    pr_number       INTEGER,
    run_id          UUID,                                -- links to sdlc_runs.id (nullable)
    max_cvss        FLOAT       NOT NULL DEFAULT 0.0,
    critical_count  INTEGER     NOT NULL DEFAULT 0,
    high_count      INTEGER     NOT NULL DEFAULT 0,
    total_findings  INTEGER     NOT NULL DEFAULT 0,
    blocked         BOOLEAN     NOT NULL DEFAULT FALSE,
    findings_json   JSONB       NOT NULL DEFAULT '[]',  -- full findings array
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sec_scan_repo       ON security_scan_results(repo);
CREATE INDEX IF NOT EXISTS idx_sec_scan_pr         ON security_scan_results(repo, pr_number);
CREATE INDEX IF NOT EXISTS idx_sec_scan_run        ON security_scan_results(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sec_scan_blocked    ON security_scan_results(blocked) WHERE blocked = TRUE;
CREATE INDEX IF NOT EXISTS idx_sec_scan_scanned_at ON security_scan_results(scanned_at DESC);
"""
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_PART_H_DDL))
            conn.commit()
        print("  ok Part H DDL: security_scan_results table + indexes")
    except Exception as exc:
        print(f"  ! Part H DDL error: {exc}")

    # ── Part I: RBAC/ABAC + Product Ontology ─────────────────────────────────
    _run_part_i()

    # ── Part K: RAG product_id column ────────────────────────────────────────
    _run_part_k()

    # ── Part L: Threads v2, KB governance, Tool governance, Budget ───────────
    _run_part_l()

    # ── Part J: Partition conversion (must run after all DDL above) ───────────
    _convert_to_partitioned_tables()

    # ── Part M: RBAC/ABAC Phase — org_tree, dept_product_mappings, ad_level ───
    _run_part_m()

    # ── Part N2: Pin chats ────────────────────────────────────────────────────
    _part_n2_pin_chats()

    # ── Part N3: Feedback context columns ────────────────────────────────────
    _part_n3_feedback_context()

    # ── Part O: Migrate public.* → ainxt.* (schema move, idempotent) ─────────
    _part_o_migrate_public_to_ainxt()

    # ── Part P1: 2026-03-26 — Two-level MCP approval (is_critical, registered_by) ──
    _part_p1_mcp_two_level_approval()

    # ── Part P2: 2026-03-26 — Chat message token detail (in_tok, out_tok, latency) ──
    _part_p2_message_token_detail()

    # ── Part P3: 2026-03-26 — knowledge_docs missing columns (chunks, governance, etc.) ──
    _part_p3_knowledge_docs_chunks()

    # ── Part P4: 2026-03-30 — External MCP server registry + tool audit log ──
    _part_p4_mcp_external_servers()

    # ── Part P5: 2026-04-03 — Agent catalog favorites + agent-scoped KB docs ──
    _part_p5_agent_catalog()

    # ── Part P6: 2026-04-07 — Semantic cache (L2) + Semantic memory (L3) ──────
    _part_p6_semantic_cache_and_memory()

    # ── Part P7: 2026-04-07 — Semantic memory v2: scope, dedup, hit_count ──────
    _part_p7_semantic_memory_v2()

    # ── Part Q1: 2026-04-08 — user_id on conversations + agent_runs ────────────
    _part_q1_memory_user_scope()

    # ── Part Q2: 2026-04-08 — Model governance: dept_model_permissions table ───
    _part_q2_model_governance()

    # ── Part Q3: 2026-04-08 — Eval scores table for per-response quality ────────
    _part_q3_eval_scores()

    # ── Part R1: 2026-04-08 — Client source audit (platform/cli/ide traceability) ─
    _part_r1_client_source_audit()

    # ── Part R2: 2026-04-13 — Per-user IDE API keys ──────────────────────────
    _part_r2_user_api_keys()

    # ── Part S1: 2026-04-15 — Code Knowledge Graph table ─────────────────────
    _part_s1_code_knowledge_graph()

    # ── Part S2: 2026-04-15 — git_url on repo_index_status (IDE repo resolve) ─
    _part_s2_ide_repo_resolve()

    # ── Part S3: 2026-04-15 — Postgres-durable budget usage totals ────────────
    _part_s3_budget_pg_totals()

    # ── Part S4: 2026-04-16 — Sandbox image tracking on repo_index_status ─────
    _part_s4_sandbox_image()

    # ── Part S5: 2026-04-18 — User-level model access overrides ──────────────
    _part_s5_user_model_perms()

    # ── Part S6: 2026-04-19 — created_at on skills_pg and workflows_pg ────────
    _part_s6_skill_workflow_created_at()
    _part_s7_agent_dynamic_config()

    # ── Part S8: 2026-04-19 — generated_documents table (doc generation) ─────
    _part_s8_generated_documents()

    # ── Part S9: 2026-04-21 — Scaling indexes ────────────────────────────────
    _part_s9_scaling_indexes()

    # ── Part S10: 2026-04-21 — BM25 GIN index on document_embeddings.content ──
    _part_s10_bm25_gin()

    # ── Part S11: 2026-04-21 — Fix BM25 GIN index (drop chr(0) expression) ────
    _part_s11_bm25_gin_fix()

    # ── Part S12: 2026-04-22 — Backfill product_id/department from metadata JSONB ─
    _part_s12_product_dept_backfill()

    # ── Part S13: 2026-04-23 — Add skill_type to skills_pg ───────────────────────
    _part_s13_skill_behavioral_type()

    # ── Part S14: 2026-04-29 — Add build_root to repo_index_status ───────────────
    _part_s14_build_root()

    # ── Part S15: 2026-05-03 — Add default_branch to projects_pg ─────────────────
    _part_s15_projects_default_branch()

    # ── Part S16: 2026-05-03 — Universal Connector Framework ──────────────────
    _part_s16_connector_framework()

    # ── Part S17: 2026-05-03 — Connector access control policy columns ────────
    _part_s17_connector_access_control()

    # ── Part S18: 2026-05-04 — User API keys table (IDE + service accounts) ──
    _part_s18_user_api_keys()

    # ── Part S19: 2026-05-05 — SDLC build pipeline tables ───────────────────
    _part_s19_build_pipeline()

    # ── Part S20: 2026-05-05 — workspace_synced_at on repo_index_status ─────
    _part_s20_workspace_synced_at()

    # ── Part S21: 2026-05-05 — branch on repo_index_status ───────────────────
    _part_s21_repo_branch()

    # ── Part T1: 2026-05-07 — Model catalog update (gpt-5.4, gpt-5-5, opus) ──
    _part_t1_model_catalog_2026_05_07()

    # ── Part T1b: 2026-07-28 — Model catalog update (gpt-5.6-terra, gpt-5.6-luna) ──
    _part_t1b_model_catalog_gpt56_2026_07_28()

    # ── Part T2: 2026-05-09 — Compression metrics table ─────────────────────
    _part_t2_compress_metrics_2026_05_09()

    # ── Part T3: 2026-05-19 — SDLC multi-repo sibling table ─────────────────
    _part_t3_sdlc_multi_repo_2026_05_19()

    # ── Part T4: 2026-05-25 — Prompt cache token columns on model_usages ────
    _part_t4_cache_token_columns_2026_05_25()

    # ── Part X1: 2026-07-28 — source_channel on model_usages (channel util) ──
    _part_x1_source_channel_2026_07_28()

    # ── Part U1: 2026-06-02 — SDLC stage artifacts table ────────────────────
    _part_u1_sdlc_stage_artifacts_2026_06_02()

    # ── Part W-J: 2026-06-04 — SDLC event-stream dedupe (idempotency key) ───
    _part_w_j_sdlc_event_dedupe_2026_06_04()

    # ── Part U2: 2026-06-05 — SDLC HOD budget tracking columns ─────────────
    _part_u2_sdlc_hod_budget_2026_06_05()

    # ── Part T3: 2026-05-21 — Chat.rag_mode column ──────────────────────────
    _part_t3_chat_rag_mode_2026_05_21()

    # ── Part T4: 2026-05-21 — users.custom_about_user / custom_response_style
    _part_t4_user_custom_instructions_2026_05_21()

    # ── Part T5: 2026-05-21 — message_versions (edit + branch) ──────────────
    _part_t5_message_versions_2026_05_21()

    # ── Part T6: 2026-05-21 — chat_artifacts (Canvas) ───────────────────────
    _part_t6_chat_artifacts_2026_05_21()

    # ── Part T7: 2026-05-21 — chat_shares (public share links) ──────────────
    _part_t7_chat_shares_2026_05_21()

    # ── Part T8: 2026-05-21 — prompt_templates (saved prompts) ──────────────
    _part_t8_prompt_templates_2026_05_21()

    # ── Part S23: 2026-05-20 — Drop file_data column from knowledge_docs ────────
    # file_data stored raw file bytes for pre-approval download. Download
    # functionality has been removed — column is no longer written or read.
    _part_s23_drop_file_data()

    # ── Part T4: 2026-05-25 — Prompt cache token columns on model_usages ────
    _part_t4_cache_token_columns_2026_05_25()
    # ── Part S24: 2026-05-20 — workspace_messages partitioned table (Option B) ─
    _part_s24_workspace_messages()

    # ── Part S25: 2026-05-29 — workspace_messages column rename (sql/workspace_db.sql fix) ─
    _part_s25_workspace_messages_col_rename()

    # ── Part U1: 2026-06-02 — KB Phase 1 scope metadata ─────────────────────
    _part_u1_kb_scope_metadata_2026_06_02()

    # ── Part U2: 2026-06-02 — KB Phase 2 section-aware chunking ─────────────
    _part_u2_kb_section_chunking_2026_06_02()

    # ── Part U3: 2026-06-03 — KB Phase 4 version cascade + kb_edges ─────────
    _part_u3_kb_version_cascade_2026_06_03()

    # ── Part U4: 2026-06-03 — KB Phase 5 canonical entity registry ──────────
    _part_u4_kb_entities_2026_06_03()

    # ── Part U5: 2026-06-03 — document_embeddings.status (ACTIVE/DEPRECATED) ─
    # Phase 1 closure: deterministic active-version filter at chunk level so
    # deprecated specs never bleed into Fast/Coverage retrieval.
    _part_u5_kb_chunk_status_2026_06_03()

    # ── Part U6: 2026-06-03 — chats scope columns (per-chat product/version) ─
    # Server-derived scope (§7) — Chat row carries the deterministic
    # {product_id, domain, spec_version, kb_doc_id} that /ask injects into
    # _user_ctx['scope_filter'] and _user_ctx['kb_doc_id'].
    _part_u6_chat_scope_2026_06_03()

    # ── Part U7: 2026-06-03 — chat_messages.coverage_trace ──────────────────
    # Phase 3 transparency persistence (kn_rewrite.md §8x): the coverage
    # badge survives a reload because the trace dict is stored on the
    # assistant message, not just streamed on the SSE __meta__ frame.
    _part_u7_chat_message_coverage_trace_2026_06_03()

    # ── Part U8: 2026-06-03 — drop knowledge_docs.git_ref (no SCM mirror) ───
    # The local filesystem at KB_DOC_STORAGE_PATH is the single SoR for full
    # doc bodies. No GitLab/GitHub mirror participates in retrieval. Drop
    # the legacy git_ref column from existing installs; new installs never
    # created it.
    _part_u8_drop_knowledge_docs_git_ref_2026_06_03()

    # ── Part U9: 2026-06-03 — drop knowledge_docs.object_store_uri ──────────
    # The full doc body is now stored at KB_DOC_STORAGE_PATH/<doc_id>.md
    # on the local filesystem. The URI is implicit from doc_id — no column
    # needed. Drop the legacy object_store_uri column from existing installs.
    _part_u9_drop_knowledge_docs_object_store_uri_2026_06_03()

    # ── Part U10: 2026-06-02 — SDLC stage artifacts table ───────────────────
    # Merged from uat. Original label "U1" on the uat branch was renumbered
    # here to keep the U1–U9 KB-migration sequence contiguous and avoid a
    # duplicate U1 label.
    _part_u10_sdlc_stage_artifacts_2026_06_02()

    # ── Part W-J: 2026-06-04 — SDLC event-stream dedupe (idempotency key) ───
    _part_w_j_sdlc_event_dedupe_2026_06_04()

    # ── Part S26: 2026-06-04 — rag_mode context isolation columns ─────────────
    _part_s26_rag_mode_isolation()

    # ── Part U11: 2026-06-08 — chunk hierarchy metadata ──────────────────────
    # docx §8 "Structured Document Storage": every chunk row carries
    # doc_name, section_name (leaf heading), page_number, source_type
    # so citation rendering is a one-step read (no cross-DB join) and
    # Fast-tier filtering by source_type works without a join either.
    _part_u11_kb_chunk_hierarchy_2026_06_08()

    # ── Part U12: 2026-06-08 — BM25 exact-term tsvector ──────────────────────
    # docx §4 "BM25 preserves exact terminology": a parallel 'simple'
    # tsvector (no stemming, no stop-word removal) lives alongside the
    # english tsvector so identifiers like RBI/2024-25/12 and quoted
    # phrases survive. keyword_search routes phrase/identifier queries
    # to this column via phraseto_tsquery('simple', ...).
    _part_u12_kb_simple_tsv_2026_06_08()

    # ── Part U13: 2026-06-08 — knowledge_docs source_type + original_ext ─────
    # source_type captured at upload (BRD/FSD/TPMC_DECISION/RBI_CIRCULAR/
    # ARCHITECTURE/SPEC/OTHER). original_ext lets the citation footer link
    # to the binary original at KB_DOC_STORAGE_PATH/<doc_id>.<ext>.
    _part_u13_kdocs_source_type_2026_06_08()

    # ── Part U14: 2026-06-08 — kb_edges relation index + CHECK ───────────────
    # docx §10 "Mandate Retry → approved_by TPMC → implemented_in Settlement
    # → governed_by RBI": the relation sub-kind lives in props.relation
    # (schemaless, matches kb_entity_worker's existing convention). Add a
    # functional index + a CHECK pinning the allowed values.
    _part_u14_kb_edges_relation_2026_06_08()

    # ── Part V1: 2026-06-11 — managed_endpoints (named proxy endpoints) ────────
    _part_v1_managed_endpoints_2026_06_11()

    # ── Part V2: 2026-06-11 — managed_endpoints schema revision ─────────────
    # Replaces token/allowed_models columns with env_key_name (LiteLLM key ref)
    _part_v2_managed_endpoints_revision_2026_06_11()

    # ── Part V3: 2026-06-12 — managed_endpoints API key + env toggle ─────────
    # Adds api_key_id FK → user_api_keys and use_env_key boolean toggle
    _part_v3_managed_endpoints_apikey_2026_06_12()

    # ── Part V4: 2026-06-22 — endpoint usage tracking + tool calls toggle ────
    _part_v4_managed_endpoints_2026_06_22()

    # ── Part V1: 2026-06-17 — pin SDLC run to one base commit (workspace consistency) ──
    _part_v1_sdlc_base_sha_2026_06_17()

    # ── Part W1: 2026-06-17 — scanned PDF upload support ─────────────────────
    # Adds is_scanned_pdf BOOLEAN to knowledge_docs so image-only PDFs can be
    # uploaded and OCR+compliance deferred to activate_doc() post-approval.
    _part_w1_scanned_pdf_flag_2026_06_17()

    # ── Part W1: 2026-06-17 — scanned PDF upload support ─────────────────────
    # Adds is_scanned_pdf BOOLEAN to knowledge_docs so image-only PDFs can be
    # uploaded and OCR+compliance deferred to activate_doc() post-approval.
    _part_w1_scanned_pdf_flag_2026_06_17()

    # ── Part U1: 2026-05-30 — Cowork parity (roles, memory, scheduling, policy)
    _part_u1_cowork_parity_2026_05_30()

    # ── Part U2: 2026-05-30 — Cowork usage analytics + computer-use audit ────
    _part_u2_cowork_usage_2026_05_30()

    # ── Part U3: 2026-05-30 — Cowork marketplace publishing + mobile dispatch ─
    _part_u3_cowork_enterprise_2026_05_30()

    # ── Part U4: 2026-05-30 — cowork_usage rollup + BRIN (scaling) ───────────
    _part_u4_cowork_usage_scale_2026_05_30()

    # ── Part U5: 2026-05-31 — server Cowork projects + project-linked schedules
    _part_u5_cowork_projects_2026_05_31()

    # ── Part U6: 2026-05-31 — server-persisted Cowork conversations (chat history)
    _part_u6_cowork_conversations_2026_05_31()

    # ── Part U7: 2026-05-31 — HASH(user_id) partition conversations + schedules ──
    _part_u7_cowork_hash_partitions_2026_05_31()
    _part_u8_user_oauth_tokens_fix_2026_05_31()

    # ── Part U9: 2026-06-02 — doc artifact versioning (iterative editing) ──────
    _part_u9_doc_artifact_versions_2026_06_02()

    # ── Part V1: 2026-06-10 — Teams meeting intelligence + Graph audit ─────────
    _part_v1_teams_meeting_2026_06_10()

    # ── Part W1: 2026-06-11 — Unified knowledge graph (code + docs) ────────────
    _part_w1_knowledge_graph_2026_06_11()

    # ── Part W2: 2026-06-14 — Self-improving skill loop proposals ──────────────
    _part_w2_skill_proposals_2026_06_14()

    # ── Part X1: 2026-06-15 — External resource sync status (Anthropic/OpenAI) ──
    _part_x1_external_sync_2026_06_15()

    # ── Part V1: 2026-06-17 — pin SDLC run to one base commit (workspace consistency) ──
    _part_v1_sdlc_base_sha_2026_06_17()

    # ── Part V2: 2026-06-26 — SDLC Work Item columns (TICKET_NORMALIZATION) ───
    _part_v2_sdlc_work_item_2026_06_26()

    # ── Part X2: 2026-06-24 — Mixed-PDF scanned pages flag (PaddleOCR) ──────────
    _part_x2_mixed_scanned_pages_2026_06_24()

    # ── Part X3: 2026-06-29 — knowledge_docs.parse_error (activation failure detail)
    _part_x3_knowledge_docs_parse_error_2026_06_29()



    # ── Part Z5: 2026-06-30 — AiNxt Coach (coach_* tables) ──
    _part_z5_coach_2026_06_30()

    # ── Part Z6: 2026-07-08 — CLI version registry (fleet visibility for updates) ──
    _part_z6_cli_version_registry_2026_07_08()

    _part_aa1_discussions_bot_runs_2026_07_11()

    _part_aa2_discussions_mirror_2026_07_11()

    _part_aa3_discussions_comments_2026_07_11()

    _part_aa4_discussions_timestamp_fix_2026_07_12()

    _part_aa5_discussions_comment_count_2026_07_12()

    # ── Part AA6: 2026-07-24 — Endpoint multi-model allowlist (model_ids JSONB) ──
    _part_aa6_endpoint_model_ids_2026_07_24()

    # OSS GAP-18: create DBA-owned tables that were missing for OSS users
    _part_oss1_department_hod_mapping_2026_07_29()

    # OSS GAP-6: add is_temp_password column for forgot-password flow
    _part_oss2_temp_password_flag_2026_07_29()

    # ── Part OSS2: 2026-08-02 — Add employee_id column to users table ──
    _part_oss2_user_employee_id_2026_08_02()

    # -- Part AB1: 2026-08-06 -- KB Deletion History (knowledge_doc_deletions) --
    _part_ab1_kb_doc_deletions_2026_08_06()

    # ── Parts AA7–AA14 ────────────────────────────────────────────────────────
    # These eight were defined at the bottom of this file but never called from
    # here, so the last month of migrations (2026-07-23 .. 2026-08-17) never ran
    # on any fresh install. That is why ainxt.codewiki_doc_jobs did not exist and
    # GET /ainxt/v1/api/codewiki/codebases returned 500, despite Part AA8 having
    # a correct CREATE TABLE IF NOT EXISTS for it.
    _part_aa7_source_channel_remap_2026_08_03()
    _part_aa8_codewiki_docs_2026_07_23()
    _part_aa9_codewiki_docs_logs_2026_08_07()
    _part_aa10_codewiki_docs_last_commit_sha_2026_08_10()
    _part_aa11_eval_results_judge_model_2026_08_14()
    _part_aa12_eval_results_model_2026_08_14()
    _part_aa13_eval_results_platform_2026_08_15()
    _part_aa14_api_key_expires_at_2026_08_17()

    # ── OSS schema-drift fixes ───────────────────────────────────────────────
    # (_part_oss3 runs at the top of this function — the catalogue seeds need it.)
    _part_oss4_model_permissions_web_search()
    _part_oss8_users_hod_email()
    _part_oss9_user_tokens_id_default()
    _part_oss10_hod_ledger_request_columns()

def _part_oss2_user_employee_id_2026_08_02():
    """
    2026-08-02 — Add employee_id column to users table.
    Stores the numeric employeeID attribute fetched from Active Directory.
    Idempotent — safe to run multiple times.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.users
        ADD COLUMN IF NOT EXISTS employee_id VARCHAR(100) NULL;
    """)
    _run_ddl(f"""
        COMMENT ON COLUMN {DB_SCHEMA}.users.employee_id
        IS 'Numeric employeeID from Active Directory (populated by LDAP login and nightly ad_sync)';
    """)
    print("  ✓ Part OSS2: users.employee_id column (AD employeeID)")


def _part_x2_mixed_scanned_pages_2026_06_24():
    """
    2026-06-24 — Add has_mixed_scanned_pages flag to knowledge_documents.

    Supports mixed PDFs where only some pages are image-only (scanned) while
    others have selectable text. Unlike is_scanned_pdf (which flags fully-scanned
    documents with no text at upload time), this flag is set when the upload
    succeeds with partial text but some pages need PaddleOCR at activation time.

    At activation, _pick_converter() detects the scanned pages, runs PaddleOCR
    on those page ranges, and merges the results with the digital pages' text.
    Deferred compliance also runs on the merged output when this flag is True.
    """
    # NOTE: the SQLAlchemy model (db/models.py KnowledgeDocument) maps to the
    # table `knowledge_docs` — every other KB migration in this file targets
    # that name unqualified. An earlier version of this block wrote
    # `{schema}.knowledge_documents`, which added the column to the wrong (often
    # non-existent) table, leaving `knowledge_docs.has_mixed_scanned_pages`
    # missing and 500-ing the inbox pending-approvals query. Fixed to match.
    _run_ddl(
        "ALTER TABLE knowledge_docs "
        "ADD COLUMN IF NOT EXISTS has_mixed_scanned_pages BOOLEAN NOT NULL DEFAULT FALSE"
    )
    print("  ✓ Part X2: knowledge_docs.has_mixed_scanned_pages column added")


def _part_x3_knowledge_docs_parse_error_2026_06_29():
    """
    2026-06-29 — Add parse_error TEXT column to knowledge_docs.

    Stores the human-readable error message from the last failed activate_doc()
    attempt (e.g. "Embedding failed: ReadTimeout after 120s", "12 of 40 chunks
    returned zero vectors — Ollama may be overloaded").

    Set by kb_worker._rollback_status() on every rollback to PENDING_APPROVAL.
    Cleared at the start of each new activation attempt so stale errors from a
    previous run are never shown after a successful re-approval.

    Surfaced in the UI (KnowledgeBase.jsx) as a red warning banner on the doc
    card so approvers and uploaders know exactly why a document silently returned
    to PENDING_APPROVAL without needing to dig through server logs.

    Idempotent: ADD COLUMN IF NOT EXISTS.
    """
    _run_ddl(
        "ALTER TABLE knowledge_docs "
        "ADD COLUMN IF NOT EXISTS parse_error TEXT",
        "Part X3: knowledge_docs.parse_error column (activation failure detail)",
    )

def _part_w2_skill_proposals_2026_06_14():
    """2026-06-14 — Self-improving skill loop (propose → HITL approval).

    Durable audit record of every auto-synthesized skill PROPOSAL and its HITL
    outcome. High-volume run *signatures* live ephemerally in Redis db=1
    (skill_loop_store, 7-day window); only signatures that cross the repeat
    threshold and become a proposal are promoted to this table.

    A proposal that yields a skill creates a SkillRecord in skills_pg as
    PENDING_APPROVAL (never PRODUCTION) — the existing governance state machine
    is the only path to PRODUCTION. Statuses:
      PROPOSED              — synthesized, awaiting skill creation/compliance
      SKILL_CREATED         — a PENDING_APPROVAL SkillRecord was created
      DISCARDED_COMPLIANCE  — synthesized code tripped the compliance gate; no skill
      DISCARDED_DUP         — deduped against an existing skill / open proposal

    The partial unique index allows the SAME signature to be re-proposed after a
    prior proposal resolves, but blocks two concurrent open proposals for it.
    Idempotent via CREATE TABLE/INDEX IF NOT EXISTS.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.skill_proposals (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            signature            VARCHAR(64)  NOT NULL,
            proposed_name        VARCHAR(255) NOT NULL,
            skill_name           VARCHAR(255),
            skill_type           VARCHAR(20)  NOT NULL DEFAULT 'execution',
            source               VARCHAR(40)  NOT NULL,
            department           VARCHAR(255),
            occurrence_count     INTEGER      NOT NULL DEFAULT 0,
            representative_prompt TEXT,
            tool_sequence        JSONB        NOT NULL DEFAULT '[]',
            synthesized_code     TEXT,
            compliance_findings  JSONB        NOT NULL DEFAULT '[]',
            status               VARCHAR(30)  NOT NULL DEFAULT 'PROPOSED',
            created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            resolved_at          TIMESTAMPTZ
        );
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_skill_proposals_sig ON {DB_SCHEMA}.skill_proposals(signature);")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_skill_proposals_status ON {DB_SCHEMA}.skill_proposals(status);")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_skill_proposals_dept ON {DB_SCHEMA}.skill_proposals(department);")
    _run_ddl(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_proposal_open "
        f"ON {DB_SCHEMA}.skill_proposals(signature) WHERE status = 'PROPOSED';"
    )

    _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
    try:
        _run_ddl(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.skill_proposals TO {_app_user};")
    except Exception:
        pass
    print("  ok Part W2: skill_proposals created")


def _part_u9_doc_artifact_versions_2026_06_02():
    """2026-06-02 — Iterative document editing (Canvas/Pages parity).

    Adds artifact_id + version to generated_documents so successive build_document
    calls that share an artifact_id are tracked as VERSIONS of one logical
    document. Backfills artifact_id = id (each existing doc is its own v1).
    Idempotent via ADD COLUMN IF NOT EXISTS.
    """
    _run_ddl("""
             ALTER TABLE generated_documents ADD COLUMN IF NOT EXISTS artifact_id VARCHAR(36);
             ALTER TABLE generated_documents ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
             UPDATE generated_documents SET artifact_id = id WHERE artifact_id IS NULL;
             CREATE INDEX IF NOT EXISTS idx_gendoc_artifact ON generated_documents(artifact_id, version);
             """, label="Part U9: generated_documents artifact versioning")
    print("  ok Part U9: generated_documents artifact_id + version columns")


def _part_v1_teams_meeting_2026_06_10():
    """2026-06-10 — Teams meeting intelligence + post-meeting automation + Graph audit.

    Backs the Microsoft Teams + Office integration (scope doc §4/§5/§7). Three tables,
    all schema-qualified + FK-less (user_id is the JWT `sub`, a plain string — matching
    ainxt.user_oauth_tokens / ainxt.user_tokens):

      • meeting_jobs       — one row per ended online meeting; UNIQUE(meeting_id) gives
                             idempotent dedup so poll + webhook + manual triggers never
                             double-process the same meeting.
      • graph_subscriptions— Graph change-notification subscriptions (Phase 4); created
                             now so the renewal/reconcile worker has its table even while
                             polling is the primary detector.
      • graph_audit_log    — tamper-evident boundary log. Stores ONLY a SHA-256 data_hash
                             of each payload (NEVER the raw transcript/summary/prompt — the
                             data-sovereignty rule), a per-stream prev_hash hash-chain, and
                             an HMAC signature (core/audit_signer.sign_event). UNIQUE(stream,
                             seq) enforces the monotonic per-stream sequence.

    Idempotent via CREATE TABLE/INDEX IF NOT EXISTS.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.meeting_jobs (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            meeting_id     VARCHAR(512) NOT NULL,
            organizer_id   VARCHAR(255),
            subject        TEXT,
            status         VARCHAR(40) NOT NULL DEFAULT 'pending',
            transcript_id  VARCHAR(512),
            detected_via   VARCHAR(20),
            error          TEXT,
            attempts       INTEGER NOT NULL DEFAULT 0,
            meeting_end    TIMESTAMPTZ,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (meeting_id)
        );
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_meeting_jobs_status ON {DB_SCHEMA}.meeting_jobs(status, created_at);")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_meeting_jobs_organizer ON {DB_SCHEMA}.meeting_jobs(organizer_id);")

    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.graph_subscriptions (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            subscription_id  VARCHAR(255) UNIQUE,
            resource         VARCHAR(500) NOT NULL,
            change_type      VARCHAR(120) NOT NULL,
            client_state     VARCHAR(128) NOT NULL,
            notification_url TEXT NOT NULL,
            expires_at       TIMESTAMPTZ NOT NULL,
            status           VARCHAR(30) NOT NULL DEFAULT 'active',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_graph_subs_expiry ON {DB_SCHEMA}.graph_subscriptions(expires_at) WHERE status = 'active';")

    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.graph_audit_log (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            stream      VARCHAR(160) NOT NULL,
            seq         BIGINT NOT NULL,
            event       VARCHAR(80) NOT NULL,
            user_id     VARCHAR(255),
            resource    VARCHAR(512),
            data_hash   VARCHAR(64) NOT NULL,
            meta        JSONB NOT NULL DEFAULT '{{}}',
            prev_hash   VARCHAR(64),
            signature   VARCHAR(64) NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (stream, seq)
        );
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_graph_audit_stream ON {DB_SCHEMA}.graph_audit_log(stream, seq);")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_graph_audit_event ON {DB_SCHEMA}.graph_audit_log(event, created_at);")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_graph_audit_created ON {DB_SCHEMA}.graph_audit_log USING BRIN(created_at);")

    _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
    for _tbl in ("meeting_jobs", "graph_subscriptions", "graph_audit_log"):
        try:
            _run_ddl(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.{_tbl} TO {_app_user};")
        except Exception:
            pass
    print("  ok Part V1: meeting_jobs + graph_subscriptions + graph_audit_log created")


def _part_oss8_users_hod_email():
    """
    OSS fix — add users.hod_email (upgrade path for pre-existing databases).

    auth/dependencies.py, auth/rbac.py and routers/budget_router.py all query
    ``users.hod_email``, but the User model never declared it, so it existed only
    where a DBA had added it by hand. GET /ainxt/v1/api/budget/admin/hods
    returned 500 with ``column "hod_email" does not exist`` on every fresh
    install. db/models.py now declares it; this covers existing databases.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.users
            ADD COLUMN IF NOT EXISTS hod_email VARCHAR(255);
        CREATE INDEX IF NOT EXISTS ix_users_hod_email
            ON {DB_SCHEMA}.users (hod_email)
    """, "Part OSS8: users.hod_email")


def _part_oss9_user_tokens_id_default():
    """
    OSS fix — add server-side UUID default to user_tokens.id.

    The UserToken ORM model (db/models.py) declares ``id`` with a Python-side
    ``default=_uuid`` only. The ``upsert_token`` endpoint in
    routers/profile_router.py uses raw SQL to INSERT into user_tokens without
    naming ``id``, so the Python default is never invoked and PostgreSQL
    rejects the NULL with a NOT NULL violation.

    This is the same class of bug that _part_oss6_uuid_pk_server_defaults()
    fixes generically, but that function may have run before the user_tokens
    table existed (it was created by the ORM model, not by raw DDL), or the
    column type detection may have missed it. A targeted ALTER is safer.
    """
    _run_ddl(
        f"ALTER TABLE {DB_SCHEMA}.user_tokens ALTER COLUMN id SET DEFAULT gen_random_uuid()",
        "Part OSS9: user_tokens.id server-side default",
    )


def _part_oss10_hod_ledger_request_columns():
    """
    OSS fix — add the budget-increase-request-lifecycle columns to
    hod_allocation_ledger, and delegated_to to department_hod_mapping.

    _part_oss1_department_hod_mapping_2026_07_29() (above) creates
    hod_allocation_ledger with only its ORIGINAL 14 columns (matching the
    pre-2026-07-23 shape of db.models.HodAllocationLedger). The model was
    extended on 2026-07-23 with the request-lifecycle columns
    (justification/status/requested_extra_cost_usd/requester_*/
    current_*_cost_usd/resolved_at/approved_by*/delegated_to) plus
    endpoint_spend_usd (2026-08+, managed-endpoint cloud spend), but no
    migration ever ALTERed the table to match — so on any DB where Part OSS1
    ran before this fix (or ran once and was never re-run), the table is
    permanently stuck on the old 14-column shape.

    This is the actual root cause of budget_router.request_increase() and
    approve/reject/list-requests failing with e.g.
    ``psycopg2.errors.UndefinedColumn: column "justification" of relation
    "hod_allocation_ledger" does not exist`` even after a user's department
    correctly resolves to an HOD via department_hod_mapping — the HOD
    resolution succeeds, but the INSERT that files the actual request dies.

    department_hod_mapping.delegated_to (comma-separated delegatee emails,
    read/written by resolve_delegates_for_hod/set_hod_delegates in
    store/budget_store.py) has the same gap — Part OSS1's CREATE TABLE never
    declared it either.

    Separately, Part OSS1's original DDL declared amount_usd,
    cap_at_time_usd and consumed_after_usd as NOT NULL with no default —
    correct for the original allocate/approve-only rows it was designed for,
    but store.budget_store.request_budget_increase() inserts 'pending'
    request rows with these three left NULL (unknown until the request is
    approved), matching db.models.HodAllocationLedger's nullable=True. Drop
    the NOT NULL constraint on all three to match the ORM model.

    Same class of bug on budget_configs: _run_part_l()'s CREATE TABLE (and
    _part_s3_budget_pg_totals()'s later ALTER) never added base_cost_usd /
    extra_cost_usd / winner_extra_usd / winner_origin_period, even though
    db.models.BudgetConfig has declared them (with NOT NULL + defaults on
    the first three) since the HOD budget-increase feature shipped. Without
    them, approve_budget_request()'s read of budget_configs during approval
    fails with ``UndefinedColumn: column "base_cost_usd" does not exist`` —
    so a request could be correctly routed and filed (once the ledger gap
    above is fixed) but still could never be approved.

    Idempotent (ADD COLUMN IF NOT EXISTS / DROP NOT NULL / CREATE ... IF NOT
    EXISTS) — safe to run against a table that already matches some or all
    of this shape.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.hod_allocation_ledger
            ADD COLUMN IF NOT EXISTS justification            TEXT,
            ADD COLUMN IF NOT EXISTS status                   VARCHAR(20) NOT NULL DEFAULT 'approved',
            ADD COLUMN IF NOT EXISTS requested_extra_cost_usd NUMERIC(12,6),
            ADD COLUMN IF NOT EXISTS requester_email          VARCHAR(255),
            ADD COLUMN IF NOT EXISTS requester_name           VARCHAR(255),
            ADD COLUMN IF NOT EXISTS requester_department     VARCHAR(255),
            ADD COLUMN IF NOT EXISTS current_base_cost_usd    NUMERIC(12,6),
            ADD COLUMN IF NOT EXISTS current_extra_cost_usd   NUMERIC(12,6),
            ADD COLUMN IF NOT EXISTS resolved_at              TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS approved_by              VARCHAR(255),
            ADD COLUMN IF NOT EXISTS approved_by_name         VARCHAR(255),
            ADD COLUMN IF NOT EXISTS delegated_to             VARCHAR(255),
            ADD COLUMN IF NOT EXISTS endpoint_spend_usd       NUMERIC(12,6) NOT NULL DEFAULT 0,
            ALTER COLUMN amount_usd         DROP NOT NULL,
            ALTER COLUMN cap_at_time_usd    DROP NOT NULL,
            ALTER COLUMN consumed_after_usd DROP NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS uq_hal_endpoint_spend_period
            ON {DB_SCHEMA}.hod_allocation_ledger (lower(hod_email), period_yyyymm)
            WHERE action = 'endpoint_spend';

        ALTER TABLE {DB_SCHEMA}.department_hod_mapping
            ADD COLUMN IF NOT EXISTS delegated_to TEXT;

        ALTER TABLE {DB_SCHEMA}.budget_configs
            ADD COLUMN IF NOT EXISTS base_cost_usd        NUMERIC(12,6) NOT NULL DEFAULT 50.0,
            ADD COLUMN IF NOT EXISTS extra_cost_usd       NUMERIC(12,6) NOT NULL DEFAULT 0.0,
            ADD COLUMN IF NOT EXISTS winner_extra_usd     NUMERIC(12,6) NOT NULL DEFAULT 0.0,
            ADD COLUMN IF NOT EXISTS winner_origin_period VARCHAR(7);
    """, "Part OSS10: hod_allocation_ledger request-lifecycle columns + department_hod_mapping.delegated_to + budget_configs winner/base/extra columns")


def _part_oss7_mirror_orm_defaults():
    """
    OSS fix — mirror db/models.py column defaults into real database defaults.

    SQLAlchemy applies ``default=`` in Python, so it does not exist in the
    database. Every raw-SQL INSERT in this file that omits such a column fails
    with ``null value in column "X" violates not-null constraint``. That is one
    bug class with many faces: it emptied the model cost catalogue
    (model_rate_table via Parts L/T1/T1b) and blocked the default budget rows
    (budget_configs.base_cost_usd, monthly_limit_usd, extra_cost_usd,
    winner_extra_usd, model_allowlist, created_at, updated_at via Part L).

    Rather than patch every INSERT, copy each NOT NULL column's declared default
    into a server-side DEFAULT:
      * scalar literals (bool / int / float / str)  -> that literal
      * list / dict                                 -> the JSON literal
      * callable on a date/time column              -> NOW()
      * ``default=list`` / ``default=dict``          -> '[]' / '{}'
    Columns that already have a server default are left alone. Idempotent.
    """
    from sqlalchemy import text as _text
    from sqlalchemy import DateTime, Date
    import json as _json
    applied = 0
    try:
        with engine.connect() as conn:
            for table in Base.metadata.tables.values():
                for col in table.columns:
                    if col.nullable or col.server_default is not None:
                        continue
                    default = getattr(col, "default", None)
                    if default is None:
                        continue
                    val = getattr(default, "arg", None)
                    literal = None
                    if callable(val):
                        # e.g. default=_now / default=_uuid / default=list
                        if isinstance(col.type, (DateTime, Date)):
                            literal = "NOW()"
                        else:
                            # SQLAlchemy wraps callable defaults, so `val` is not
                            # the original `list`/`dict` object. Invoke it and use
                            # the result only when it is an empty container --
                            # never for uuid4/now-style callables, where baking one
                            # produced value in as the DEFAULT would give every row
                            # the same value. Covers JSONB columns declared
                            # `default=list` (e.g. budget_configs.model_allowlist).
                            try:
                                produced = val(None)
                            except Exception:
                                produced = None
                            if isinstance(produced, (list, dict)) and not produced:
                                literal = "'" + _json.dumps(produced) + "'"
                    elif isinstance(val, bool):
                        literal = "TRUE" if val else "FALSE"
                    elif isinstance(val, (int, float)):
                        literal = repr(val)
                    elif isinstance(val, str):
                        literal = "'" + val.replace("'", "''") + "'"
                    elif isinstance(val, (list, dict)):
                        literal = "'" + _json.dumps(val).replace("'", "''") + "'"
                    if literal is None:
                        continue
                    try:
                        conn.execute(_text(
                            f'ALTER TABLE {DB_SCHEMA}."{table.name}" '
                            f'ALTER COLUMN "{col.name}" SET DEFAULT {literal}'
                        ))
                        conn.commit()
                        applied += 1
                    except Exception:
                        conn.rollback()
        print(f"  ok Part OSS7: mirrored {applied} ORM default(s) into the database")
    except Exception as exc:
        print(f"  ! Part OSS7 error: {exc}")


def _part_oss6_uuid_pk_server_defaults():
    """
    OSS fix — server-side defaults for UUID primary keys.

    db/models.py declares primary keys as
    ``Column(UUID(as_uuid=False), primary_key=True, default=_uuid)``. That
    default is applied by SQLAlchemy in Python, so it does not exist in the
    database. Every raw-SQL seeding path in this file that INSERTs without
    naming ``id`` therefore fails with
    ``null value in column "id" ... violates not-null constraint``.

    Observed on model_rate_table (Parts L, T1, T1b — the model cost catalogue
    was left completely empty) and budget_configs (Part L). Rather than patch
    each INSERT, give every such column a real database default.
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            rows = conn.execute(_text("""
                SELECT c.table_name, c.column_name, c.data_type
                  FROM information_schema.columns c
                  JOIN information_schema.key_column_usage k
                    ON  k.table_schema = c.table_schema
                    AND k.table_name   = c.table_name
                    AND k.column_name  = c.column_name
                  JOIN information_schema.table_constraints t
                    ON  t.constraint_name  = k.constraint_name
                    AND t.table_schema     = k.table_schema
                    AND t.constraint_type  = 'PRIMARY KEY'
                 WHERE c.table_schema = :schema
                   AND c.column_default IS NULL
                   AND c.is_nullable = 'NO'
                   AND (c.data_type = 'uuid'
                        OR (c.data_type = 'character varying'
                            AND c.character_maximum_length = 36))
            """), {"schema": DB_SCHEMA}).fetchall()
            fixed = 0
            for table_name, column_name, data_type in rows:
                expr = ("gen_random_uuid()" if data_type == "uuid"
                        else "gen_random_uuid()::text")
                try:
                    conn.execute(_text(
                        f'ALTER TABLE {DB_SCHEMA}."{table_name}" '
                        f'ALTER COLUMN "{column_name}" SET DEFAULT {expr}'
                    ))
                    conn.commit()
                    fixed += 1
                except Exception:
                    conn.rollback()
        print(f"  ok Part OSS6: server-side UUID defaults set on {fixed} primary key(s)")
    except Exception as exc:
        print(f"  ! Part OSS6 error: {exc}")


def _part_oss5_chat_attachments_chat_id_uuid():
    """
    OSS fix — convert chat_attachments.chat_id from VARCHAR(36) to UUID.

    Needed so Part G can create fk_chat_attachments_chat_id. On a fresh install
    db/models.py now declares the column as UUID and create_all() gets it right;
    this part is the upgrade path for databases created before that change.

    The cast is attempted only when every existing value is a valid UUID, so a
    database holding non-UUID chat_ids is reported rather than silently mangled.
    """
    _run_ddl(f"""
        DO $conv$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = '{DB_SCHEMA}' AND table_name = 'chat_attachments'
               AND column_name = 'chat_id' AND data_type <> 'uuid'
          ) THEN
            -- Nested, not combined with AND: once the column is already uuid the
            -- regex test below is a type error (`operator does not exist:
            -- uuid !~ unknown`), and Postgres evaluates the whole condition.
            IF NOT EXISTS (
              SELECT 1 FROM {DB_SCHEMA}.chat_attachments
               WHERE chat_id IS NOT NULL
                 AND chat_id::text !~ '^[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}$'
            ) THEN
              ALTER TABLE {DB_SCHEMA}.chat_attachments
                ALTER COLUMN chat_id TYPE UUID USING chat_id::uuid;
            END IF;
          END IF;
        END $conv$
    """, "Part OSS5: chat_attachments.chat_id -> UUID")


def _part_oss3_model_rate_table_id_default():
    """
    OSS fix — give model_rate_table.id a server-side default.

    db/models.py declares ``id = Column(UUID, primary_key=True, default=_uuid)``.
    That default is applied by SQLAlchemy in Python, so it does not exist in the
    database. Three separate raw-SQL seeding paths (Part L, Part T1, Part T1b)
    INSERT into model_rate_table without naming ``id`` and all failed with
    ``null value in column "id" ... violates not-null constraint``, leaving the
    model cost/pricing catalogue completely empty and spend tracking with no
    rates to work from.
    """
    # Every one of these is NOT NULL with its default declared only in
    # db/models.py (SQLAlchemy applies those in Python, so they do not exist in
    # the database). Parts L, T1 and T1b seed the catalogue with raw SQL that
    # does not name them, so each INSERT failed and the model cost/pricing
    # catalogue stayed empty — leaving spend tracking with no rates at all.
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.model_rate_table
            ALTER COLUMN id SET DEFAULT gen_random_uuid();
        ALTER TABLE {DB_SCHEMA}.model_rate_table
            ALTER COLUMN effective_from SET DEFAULT NOW();
        ALTER TABLE {DB_SCHEMA}.model_rate_table
            ALTER COLUMN created_at SET DEFAULT NOW();
        ALTER TABLE {DB_SCHEMA}.model_rate_table
            ALTER COLUMN is_free SET DEFAULT FALSE;
        ALTER TABLE {DB_SCHEMA}.model_rate_table
            ALTER COLUMN input_cost_per_1k SET DEFAULT 0.0;
        ALTER TABLE {DB_SCHEMA}.model_rate_table
            ALTER COLUMN output_cost_per_1k SET DEFAULT 0.0
    """, "Part OSS3: model_rate_table server defaults")


def _part_oss4_model_permissions_web_search():
    """
    OSS fix — add the web_search_allowed column the governance router requires.

    routers/model_governance_router.py SELECTs and INSERTs
    ``web_search_allowed`` on both dept_model_permissions and
    user_model_permissions (lines 120, 123, 198, 248, 262), but no migration and
    no ORM model ever created it. Every call to GET /ainxt/v1/api/model-governance
    and /ainxt/v1/api/budget/admin/hods returned 500 with
    ``column "web_search_allowed" does not exist`` — the Admin -> Model
    Governance screen in the shipped UI was entirely broken on a fresh install.
    """
    for _tbl in ("dept_model_permissions", "user_model_permissions"):
        _run_ddl(f"""
            ALTER TABLE {_tbl}
                ADD COLUMN IF NOT EXISTS web_search_allowed BOOLEAN NOT NULL DEFAULT FALSE
        """, f"Part OSS4: {_tbl}.web_search_allowed")


# ── Migration failure tracking ──────────────────────────────────────────────
#
# Every part used to print "! Part X error: ..." and migrate.py still exited 0,
# so a database left without columns and tables looked like a clean run to both
# a newcomer and to CI. Failures are now collected and surfaced at the end by
# run_migrations(), which exits non-zero unless MIGRATE_ALLOW_PARTIAL=true.
_MIGRATION_FAILURES: list = []

# Capture failures reported by the ~66 sites that print "  ! ..." directly
# instead of going through _run_ddl(). Without this, those failures never reached
# the end-of-run summary and migrate.py exited 0 on a broken database. Shadowing
# print() for this module is deliberate: it gives complete coverage with no risk
# of missing a site, and is confined to db/migrate.py.
_builtin_print = print


def print(*args, **kwargs):          # noqa: A001 - intentional module-local shim
    if args and isinstance(args[0], str):
        _line = args[0].lstrip()
        if _line.startswith("! "):
            _MIGRATION_FAILURES.append(_line[2:].strip()[:300])
    return _builtin_print(*args, **kwargs)




def _record_failure(label: str, detail) -> None:
    """Record a migration failure for the end-of-run summary."""
    first = str(detail).split("\n")[0].strip()[:300]
    _MIGRATION_FAILURES.append(f"{label or 'unknown'}: {first}")


_DOLLAR_TAG_RE = _re_mod.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _split_sql(sql: str) -> list:
    """
    Split a SQL script into statements, respecting dollar-quoted blocks.

    A plain ``sql.split(";")`` shreds the body of any ``DO $$ ... ; ... $$;``
    block, which made every PL/pgSQL block in this file fail with
    ``unterminated dollar-quoted string at or near "$$"``. Single- and
    double-quoted literals and ``--`` / ``/* */`` comments are also respected so
    that a semicolon inside them is not treated as a statement boundary.
    """
    out, buf, i, n = [], [], 0, len(sql)
    tag = None      # active $tag$ / $$ delimiter
    quote = None    # active ' or " literal
    while i < n:
        ch = sql[i]
        pair = sql[i:i + 2]
        if tag:
            if sql.startswith(tag, i):
                buf.append(tag); i += len(tag); tag = None
            else:
                buf.append(ch); i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:   # escaped '' or ""
                    buf.append(sql[i + 1]); i += 2; continue
                quote = None
            i += 1
            continue
        if pair == "--":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j]); i = j; continue
        if pair == "/*":
            j = sql.find("*/", i)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j]); i = j; continue
        if ch in ("'", '"'):
            quote = ch; buf.append(ch); i += 1; continue
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0); buf.append(tag); i += len(tag); continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []; i += 1; continue
        buf.append(ch); i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _run_ddl(sql: str, label: str = "") -> None:
    """Execute raw DDL on the main engine. Idempotent via IF NOT EXISTS/IF EXISTS.

    Each statement runs in its own transaction. Previously the whole batch shared
    one transaction, so a single failure poisoned it and every following
    statement died with ``InFailedSqlTransaction: current transaction is
    aborted`` — one real error was reported as many, and unrelated later
    statements never ran at all.
    """
    from sqlalchemy import text as _text
    failures = []
    for stmt in _split_sql(sql):
        try:
            with engine.connect() as conn:
                conn.execute(_text(stmt))
                conn.commit()
        except Exception as exc:
            failures.append(exc)
            print(f"  ! DDL error ({label or 'unknown'}): {exc}")
    if not failures and label:
        print(f"  ok {label}")


def _part_s1_code_knowledge_graph():
    """
    2026-04-15 — Code Knowledge Graph.
    Nodes (class/interface/module) + JSONB relations (imports/extends/implements)
    extracted by tree-sitter at index time.  graph_resolver.py queries this table
    to narrow pgvector search scope to structurally related files.
    """
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS code_graph (
            id          UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
            repo        VARCHAR(500)  NOT NULL,
            node_id     VARCHAR(1000) NOT NULL,
            node_type   VARCHAR(50)   NOT NULL,
            name        VARCHAR(500)  NOT NULL,
            file_path   TEXT          NOT NULL,
            language    VARCHAR(50),
            relations   JSONB         NOT NULL DEFAULT '[]',
            metadata    JSONB         NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_code_graph_node UNIQUE (repo, node_id)
        );
        CREATE INDEX IF NOT EXISTS idx_code_graph_repo      ON code_graph(repo);
        CREATE INDEX IF NOT EXISTS idx_code_graph_name      ON code_graph(lower(name));
        CREATE INDEX IF NOT EXISTS idx_code_graph_file      ON code_graph(repo, file_path);
        CREATE INDEX IF NOT EXISTS idx_code_graph_relations ON code_graph USING GIN(relations);
    """)


def _part_s2_ide_repo_resolve():
    """
    2026-04-15 — Add git_url column to repo_index_status.

    Maps the git remote URL stored by index_worker at indexing time (e.g.
    https://gitlab.example.com/your-org/your-service.git) to the platform's
    internal repo_name slug.  The /ide/repo/resolve endpoint queries this column
    so Kilo Code / IDE plugins can resolve their workspace git remote URL to the
    correct repo_filter without manual configuration.
    """
    _run_ddl("""
        ALTER TABLE repo_index_status ADD COLUMN IF NOT EXISTS git_url VARCHAR(1000);
        CREATE INDEX IF NOT EXISTS idx_repo_index_status_git_url
            ON repo_index_status(git_url) WHERE git_url IS NOT NULL;
    """)


def _part_s3_budget_pg_totals():
    """
    2026-04-15 — Postgres-durable budget usage totals.

    Adds user_usage_totals table so budget enforcement survives Redis outages.
    Architecture: Redis is the fast-path cache; Postgres is the source of truth.
    On Redis failure, check_budget() and increment_usage() fall back to this table.

    Also extends budget_configs with max_tokens_total and max_requests_total so
    per-user allocation limits are durable in Postgres (not just Redis hashes).
    """
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS user_usage_totals (
            user_id        VARCHAR(255) PRIMARY KEY,
            tokens_used    BIGINT       NOT NULL DEFAULT 0,
            requests_made  BIGINT       NOT NULL DEFAULT 0,
            cost_usd_spent NUMERIC(12,6) NOT NULL DEFAULT 0.0,
            last_updated   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );

        ALTER TABLE budget_configs
            ADD COLUMN IF NOT EXISTS max_tokens_total   BIGINT  DEFAULT 500000,
            ADD COLUMN IF NOT EXISTS max_requests_total INTEGER DEFAULT 1000,
            ADD COLUMN IF NOT EXISTS max_cost_usd_total NUMERIC(12,6) DEFAULT 50.0;
    """)


def _part_s4_sandbox_image():
    """
    2026-04-16 — Add sandbox_image_tag + sandbox_image_built_at to repo_index_status.

    LEGACY COLUMNS — kept for schema compatibility; no longer written or read by
    the SDLC pipeline. The old per-repo sandbox_image_builder.py approach was replaced
    by universal ainxt-builder-* images resolved via BuildManifestResolver.
    index_worker.py sets sandbox_image_tag = NULL for all new index runs.
    sdlc_state_machine.py uses BuildManifestResolver (not these columns) for image selection.
    """
    _run_ddl("""
        ALTER TABLE repo_index_status
            ADD COLUMN IF NOT EXISTS sandbox_image_tag       VARCHAR(500),
            ADD COLUMN IF NOT EXISTS sandbox_image_built_at  TIMESTAMPTZ;
    """)


# ── Part I: RBAC/ABAC + Product Ontology tables ──────────────────────────────
_PART_I_DDL = """
-- ─────────────────────────────────────────────────────────────────────────────
-- I1: ABAC columns on users table
--     band/band_level from AD title mapping; product_ids for scope isolation;
--     is_approver/is_security_team for governance gate checks
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS band             VARCHAR(5);
ALTER TABLE users ADD COLUMN IF NOT EXISTS band_level       INTEGER     NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS ad_username      VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS ad_dn            TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS ad_title         VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS department       VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS manager_dn       TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS gitlab_username  VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_approver      BOOLEAN     NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_security_team BOOLEAN     NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS product_ids      JSONB       NOT NULL DEFAULT '[]';
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_ad_sync     TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_users_ad_username ON users(ad_username) WHERE ad_username IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_band_level  ON users(band_level);
CREATE INDEX IF NOT EXISTS idx_users_department  ON users(department) WHERE department IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- I2: Title → Band mapping table
--     Band not stored in AD; map AD 'title' field via ILIKE patterns.
--     Patterns evaluated highest band_level first (ORDER BY band_level DESC).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS title_band_map (
    id            SERIAL       PRIMARY KEY,
    title_pattern VARCHAR(255) NOT NULL UNIQUE,   -- ILIKE match pattern
    band          VARCHAR(5)   NOT NULL,           -- A1 A2 B1 B2 C1 C2 D1 D2 E
    band_level    INTEGER      NOT NULL,           -- 1-9
    notes         TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_title_band_level ON title_band_map(band_level);

-- Seed default AiNxt band mappings (adjust via admin UI / SQL as needed)
INSERT INTO title_band_map (title_pattern, band, band_level, notes) VALUES
    ('%Trainee%',                        'A1', 1, 'Entry level / trainee'),
    ('%Junior%',                          'A2', 2, 'Junior staff'),
    ('%Assistant Manager%',              'B1', 3, 'AM grade'),
    ('%Deputy Manager%',                 'B2', 4, 'DM grade'),
    ('%Senior Manager%',                 'C2', 6, 'SM — must come before Manager%'),
    ('%Manager%',                        'C1', 5, 'Manager grade'),
    ('%Assistant General Manager%',      'D1', 7, 'AGM grade'),
    ('%Deputy General Manager%',         'D1', 7, 'DGM grade'),
    ('%General Manager%',                'D2', 8, 'GM grade'),
    ('%Executive Director%',             'E',  9, 'ED / C-suite'),
    ('%Chief%',                          'E',  9, 'CXO grade')
ON CONFLICT (title_pattern) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- I3: User API tokens (Local LLM / GitLab / Atlassian — user-managed)
--     NOT from credential_vault; user adds their own keys in Profile UI.
--     Value stored Fernet-encrypted using platform FERNET_KEY.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_tokens (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         VARCHAR(255) NOT NULL,
    token_type      VARCHAR(50)  NOT NULL,   -- local_llm | atlassian | gitlab
    encrypted_value TEXT         NOT NULL,
    label           VARCHAR(255),            -- friendly name e.g. 'Work GitLab'
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, token_type)
);
CREATE INDEX IF NOT EXISTS idx_user_tokens_user ON user_tokens(user_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- I4: AD user hierarchy cache (reporting chain, 4 levels deep)
--     Rebuilt nightly by workers/ad_sync.py.  Used for product auto-membership.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_hierarchy (
    user_id         VARCHAR(255) PRIMARY KEY,
    manager_ids     JSONB        NOT NULL DEFAULT '[]',  -- [L1, L2, L3, L4] email/samAccountName
    report_ids      JSONB        NOT NULL DEFAULT '[]',  -- direct reports
    last_synced_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- I5: Product ontology
--     Products are the top-level organizing entity (Rupay, UPI, SettleNxt).
--     Only C1+ users (band_level >= 5) may create products.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name               VARCHAR(255) NOT NULL UNIQUE,
    code               VARCHAR(50)  NOT NULL UNIQUE,   -- e.g. RUPAY, UPI, SNXT
    description        TEXT,
    jira_project_key   VARCHAR(50),
    confluence_space   VARCHAR(50),
    is_active          BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by         VARCHAR(255) NOT NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active);

-- ─────────────────────────────────────────────────────────────────────────────
-- I6: Product → Repo mapping (GitLab repos only)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS product_repos (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id  UUID         NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    repo_name   VARCHAR(255) NOT NULL,
    branch      VARCHAR(100) NOT NULL DEFAULT 'main',
    added_by    VARCHAR(255) NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(product_id, repo_name)
);
CREATE INDEX IF NOT EXISTS idx_product_repos_product ON product_repos(product_id);
CREATE INDEX IF NOT EXISTS idx_product_repos_repo    ON product_repos(repo_name);

-- ─────────────────────────────────────────────────────────────────────────────
-- I7: Product owners (C1+ band; owner | admin roles)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS product_owners (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id  UUID         NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    user_id     VARCHAR(255) NOT NULL,
    role        VARCHAR(50)  NOT NULL DEFAULT 'owner',   -- owner | admin
    added_by    VARCHAR(255) NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(product_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_product_owners_product ON product_owners(product_id);
CREATE INDEX IF NOT EXISTS idx_product_owners_user    ON product_owners(user_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- I8: Product membership (auto via AD reporting chain + manual)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS product_members (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id  UUID         NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    user_id     VARCHAR(255) NOT NULL,
    source      VARCHAR(50)  NOT NULL DEFAULT 'manual',  -- manual | ad_sync
    added_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(product_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_product_members_product ON product_members(product_id);
CREATE INDEX IF NOT EXISTS idx_product_members_user    ON product_members(user_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- I9: Codebase index requests (operator+ approves; admin git token used for clone)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS index_requests (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_name     VARCHAR(255) NOT NULL,
    branch        VARCHAR(100) NOT NULL DEFAULT 'main',
    product_id    UUID         REFERENCES products(id) ON DELETE SET NULL,
    requested_by  VARCHAR(255) NOT NULL,
    status        VARCHAR(50)  NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|running|done|failed
    reviewed_by   VARCHAR(255),
    review_note   TEXT,
    reviewed_at   TIMESTAMPTZ,
    error_msg     TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_index_req_status      ON index_requests(status);
CREATE INDEX IF NOT EXISTS idx_index_req_repo        ON index_requests(repo_name);
CREATE INDEX IF NOT EXISTS idx_index_req_requested   ON index_requests(requested_by);
"""


def _run_part_i():
    _engine = engine  # migrate engine (POSTGRES_MIGRATE_USER — has DDL privileges)
    from sqlalchemy import text as _text
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_PART_I_DDL))
            conn.commit()
        print("  ok Part I DDL: ABAC user columns, title_band_map, user_tokens, user_hierarchy, products, product_repos, product_owners, product_members, index_requests")
    except Exception as exc:
        print(f"  ! Part I DDL error: {exc}")


# ── Part K: RAG product_id column ────────────────────────────────────────────
_PART_K_DDL = """
-- K1: Add product_id to document_embeddings for product-scoped RAG ACL.
--     NULL = platform-wide document (accessible to all authenticated users).
--     Non-null = only accessible to members of that product.
ALTER TABLE document_embeddings
    ADD COLUMN IF NOT EXISTS product_id UUID REFERENCES products(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_doc_embed_product_id
    ON document_embeddings (product_id)
    WHERE product_id IS NOT NULL;
"""


def _run_part_k():
    _engine = engine  # migrate engine (POSTGRES_MIGRATE_USER — has DDL privileges)
    from sqlalchemy import text as _text
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_PART_K_DDL))
            conn.commit()
        print("  ok Part K DDL: document_embeddings.product_id column + index")
    except Exception as exc:
        print(f"  ! Part K DDL error: {exc}")


# ── Part L: Threads v2, KB governance, Tool governance, Budget ───────────────
_PART_L_DDL = """
-- L1: Threads v2 columns
ALTER TABLE threads_pg
    ADD COLUMN IF NOT EXISTS product_id   UUID REFERENCES products(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS agent_status VARCHAR(50),
    ADD COLUMN IF NOT EXISTS ainxt_run_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS expires_at   TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_threads_product_id ON threads_pg (product_id) WHERE product_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_threads_expires_at ON threads_pg (expires_at) WHERE expires_at IS NOT NULL;

-- L2: ThreadMessage v2 columns (nested replies, reactions, HITL)
ALTER TABLE thread_messages
    ADD COLUMN IF NOT EXISTS parent_message_id UUID,
    ADD COLUMN IF NOT EXISTS author_name       VARCHAR(255),
    ADD COLUMN IF NOT EXISTS author_band       VARCHAR(10),
    ADD COLUMN IF NOT EXISTS message_type      VARCHAR(50) NOT NULL DEFAULT 'text',
    ADD COLUMN IF NOT EXISTS hitl_status       VARCHAR(50),
    ADD COLUMN IF NOT EXISTS ainxt_run_id      VARCHAR(255),
    ADD COLUMN IF NOT EXISTS reactions         JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS edited_at         TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_tm_parent ON thread_messages (parent_message_id) WHERE parent_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tm_ainxt_run ON thread_messages (ainxt_run_id) WHERE ainxt_run_id IS NOT NULL;

-- L3: KnowledgeDocument governance columns
ALTER TABLE knowledge_docs
    ADD COLUMN IF NOT EXISTS content_hash     VARCHAR(64),
    ADD COLUMN IF NOT EXISTS visibility       VARCHAR(20)  NOT NULL DEFAULT 'PUBLIC',
    ADD COLUMN IF NOT EXISTS min_band_level   INTEGER      NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS product_id       UUID REFERENCES products(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS department_ids   JSONB        NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS status           VARCHAR(30)  NOT NULL DEFAULT 'PENDING_APPROVAL',
    ADD COLUMN IF NOT EXISTS compliance_pass  BOOLEAN,
    ADD COLUMN IF NOT EXISTS approved_by      VARCHAR(255),
    ADD COLUMN IF NOT EXISTS approved_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rejection_reason TEXT,
    ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW();

ALTER TABLE knowledge_docs
    ADD COLUMN IF NOT EXISTS uploaded_by_dept VARCHAR(255);
CREATE INDEX IF NOT EXISTS idx_kdoc_uploaded_by_dept ON knowledge_docs (uploaded_by_dept) WHERE uploaded_by_dept IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kdoc_content_hash ON knowledge_docs (content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kdoc_visibility   ON knowledge_docs (visibility);
CREATE INDEX IF NOT EXISTS idx_kdoc_status       ON knowledge_docs (status);
CREATE INDEX IF NOT EXISTS idx_kdoc_product_id   ON knowledge_docs (product_id) WHERE product_id IS NOT NULL;

-- L4: ToolSubmission table (Phase 8 — IS team governance)
CREATE TABLE IF NOT EXISTS tool_submissions (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type      VARCHAR(50) NOT NULL,
    entity_name      VARCHAR(255) NOT NULL,
    visibility       VARCHAR(20) NOT NULL DEFAULT 'PUBLIC',
    submitted_by     VARCHAR(255) NOT NULL,
    product_id       UUID REFERENCES products(id) ON DELETE SET NULL,
    status           VARCHAR(30) NOT NULL DEFAULT 'PENDING_IS_REVIEW',
    requires_is_review BOOLEAN  NOT NULL DEFAULT TRUE,
    risk_score       FLOAT,
    risk_report      TEXT,
    reviewed_by      VARCHAR(255),
    review_note      TEXT,
    reviewed_at      TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tool_sub_status      ON tool_submissions (status);
CREATE INDEX IF NOT EXISTS idx_tool_sub_submitted_by ON tool_submissions (submitted_by);
CREATE INDEX IF NOT EXISTS idx_tool_sub_expires_at  ON tool_submissions (expires_at) WHERE expires_at IS NOT NULL;

-- L5: ModelRateTable (Phase 9 — real cost tracking)
CREATE TABLE IF NOT EXISTS model_rate_table (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id           VARCHAR(100) NOT NULL,
    provider           VARCHAR(50) NOT NULL,
    input_cost_per_1k  FLOAT       NOT NULL DEFAULT 0.0,
    output_cost_per_1k FLOAT       NOT NULL DEFAULT 0.0,
    is_free            BOOLEAN     NOT NULL DEFAULT FALSE,
    effective_from     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (model_id, effective_from)
);

-- Seed model rates (current as of 2026-03)
INSERT INTO model_rate_table (model_id, provider, input_cost_per_1k, output_cost_per_1k, is_free)
VALUES
    ('claude-sonnet-4-6',   'anthropic', 0.003,  0.015,  false),
    ('gpt-5.2',             'openai',    0.005,  0.015,  false),
    ('gpt-5-mini',          'openai',    0.00015,0.0006, false),
    ('gemini-2.5-flash',    'google',    0.0001, 0.0004, false),  -- legacy / deprecated
    -- New Gemini split (replaces gemini-2.5-flash). Placeholder rates — update
    -- when official Google pricing is published. Per-1K cost matches the
    -- MODEL_COST_PER_1M table in core/model_registry.py (÷1000).
    ('gemini-3.5-flash',      'google',    0.0003,  0.0012, false),
    ('gemini-3.1-flash-lite', 'google',    0.0001,  0.0004, false),
    ('gemini-3.1-flash-image','google',    0.0003,  0.03,   false),  -- image OUTPUT tokens ~$30/1M ($0.03/1K); prior 0.0003 was a stale text rate
    ('llama3.1',            'ollama',    0.0,    0.0,    true),
    ('local-llm',           'local_llm', 0.0,    0.0,    true),
    ('neuron-llm',          'local_llm', 0.0,    0.0,    true)   -- backward compat row
ON CONFLICT (model_id, effective_from) DO NOTHING;

-- L6: BudgetConfig table (Phase 9 — band-based monthly limits)
CREATE TABLE IF NOT EXISTS budget_configs (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           VARCHAR(255) UNIQUE,
    band_level        INTEGER,
    monthly_limit_usd FLOAT       NOT NULL DEFAULT 30.0,
    model_allowlist   JSONB       NOT NULL DEFAULT '[]',
    updated_by        VARCHAR(255),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed band-based defaults: A1(1)-B2(4)=$30, C1(5)+=$50
INSERT INTO budget_configs (band_level, monthly_limit_usd) VALUES
    (1, 30.0), (2, 30.0), (3, 30.0), (4, 30.0),
    (5, 50.0), (6, 50.0), (7, 50.0), (8, 50.0), (9, 50.0)
ON CONFLICT DO NOTHING;

-- L7: Phase 9 Budget v2 — product_id on model_usages for chargeback tracking
ALTER TABLE model_usages ADD COLUMN IF NOT EXISTS product_id UUID REFERENCES products(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_model_usages_product_id ON model_usages(product_id) WHERE product_id IS NOT NULL;

-- L8: Phase 9 — default_product_id on users for per-user product tracking assignment
ALTER TABLE users ADD COLUMN IF NOT EXISTS default_product_id UUID REFERENCES products(id) ON DELETE SET NULL;
"""


def _run_part_l():
    _engine = engine  # migrate engine (POSTGRES_MIGRATE_USER — has DDL privileges)
    from sqlalchemy import text as _text
    try:
        with _engine.connect() as conn:
            conn.execute(_text(_PART_L_DDL))
            conn.commit()
        print("  ok Part L DDL: Threads v2 cols, ThreadMessage v2 cols, KB governance, ToolSubmission, ModelRateTable, BudgetConfig, model_usages.product_id")
    except Exception as exc:
        print(f"  ! Part L DDL error: {exc}")


# ── Part J: Partitioned table conversion (128 HASH partitions) ───────────────
def _convert_to_partitioned_tables():
    """
    Convert high-volume tables to HASH-partitioned variants (128 partitions).
    Idempotent — checks pg_partitioned_table before attempting conversion.

    Partitioning strategy:
      chat_messages   → HASH(chat_id)   PK(id, chat_id)   — all msgs of a chat colocated
      thread_messages → HASH(thread_id) PK(id, thread_id) — all thread msgs colocated
      model_usages    → HASH(id)        PK(id)            — even distribution; user_id nullable
      rag_access_log  → HASH(id)        PK(id)            — immutable audit; even distribution
    """
    _engine = engine  # migrate engine (POSTGRES_MIGRATE_USER — has DDL privileges)
    from sqlalchemy import text as _text

    N = 128

    def _is_partitioned(conn, table: str) -> bool:
        r = conn.execute(
            _text(
                "SELECT 1 FROM pg_partitioned_table pt "
                "JOIN pg_class c ON c.oid = pt.partrelid "
                "WHERE c.relname = :t"
            ),
            {"t": table},
        ).fetchone()
        return r is not None

    def _table_exists(conn, table: str) -> bool:
        r = conn.execute(
            _text(
                "SELECT 1 FROM information_schema.tables "
                f"WHERE table_schema = '{DB_SCHEMA}' AND table_name = :t"
            ),
            {"t": table},
        ).fetchone()
        return r is not None

    def _partition_stmts(table: str, n: int = N) -> str:
        lines = []
        for i in range(n):
            lines.append(
                f"CREATE TABLE IF NOT EXISTS {table}_p{i:03d} "
                f"PARTITION OF {table} "
                f"FOR VALUES WITH (MODULUS {n}, REMAINDER {i});"
            )
        return "\n".join(lines)

    # ── chat_messages → PARTITION BY HASH(chat_id) ────────────────────────────
    _CHAT_PARTITIONED = """
    CREATE TABLE chat_messages (
        id                       UUID    NOT NULL DEFAULT gen_random_uuid(),
        chat_id                  UUID    NOT NULL,
        role                     VARCHAR(50)  NOT NULL,
        content                  TEXT    NOT NULL,
        model_used               VARCHAR(100),
        tokens_used              INTEGER,
        cost_usd                 FLOAT,
        selected_model           VARCHAR(255),
        attachment_ids           JSONB   DEFAULT '[]',
        token_usage_deprecated   INTEGER DEFAULT 0,
        cost                     FLOAT   DEFAULT 0.0,
        language                 VARCHAR(20),
        retrieved_chunk_ids      JSONB   DEFAULT '[]',
        confidence               FLOAT   DEFAULT 0.0,
        created_at               TIMESTAMP NOT NULL DEFAULT NOW(),
        PRIMARY KEY (id, chat_id)
    ) PARTITION BY HASH (chat_id);
    CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_created
        ON chat_messages(chat_id, created_at);
    """

    # ── thread_messages → PARTITION BY HASH(thread_id) ───────────────────────
    _THREAD_PARTITIONED = """
    CREATE TABLE thread_messages (
        id          UUID    NOT NULL DEFAULT gen_random_uuid(),
        thread_id   UUID    NOT NULL,
        content     TEXT    NOT NULL,
        author      VARCHAR(255) NOT NULL DEFAULT 'user',
        mentions    JSONB   NOT NULL DEFAULT '[]',
        model_used  VARCHAR(100),
        tokens_in   INTEGER,
        tokens_out  INTEGER,
        cost_usd    FLOAT,
        latency_ms  FLOAT,
        created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
        PRIMARY KEY (id, thread_id)
    ) PARTITION BY HASH (thread_id);
    CREATE INDEX IF NOT EXISTS idx_thread_messages_thread
        ON thread_messages(thread_id);
    """

    # ── model_usages → PARTITION BY HASH(id) ─────────────────────────────────
    _USAGE_PARTITIONED = """
    CREATE TABLE model_usages (
        id            UUID    NOT NULL DEFAULT gen_random_uuid(),
        user_id       UUID,
        agent_id      VARCHAR(255),
        project_id    VARCHAR(255),
        product_id    UUID,
        endpoint      VARCHAR(255),
        model         VARCHAR(100) NOT NULL,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens  INTEGER NOT NULL DEFAULT 0,
        latency_ms    FLOAT,
        cost_usd      FLOAT,
        request_id    VARCHAR(255),
        created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
        PRIMARY KEY (id)
    ) PARTITION BY HASH (id);
    CREATE INDEX IF NOT EXISTS idx_model_usages_user_created
        ON model_usages(user_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_model_usages_request_id
        ON model_usages(request_id) WHERE request_id IS NOT NULL;
    """

    # ── rag_access_log → PARTITION BY HASH(id) ───────────────────────────────
    _RAG_LOG_PARTITIONED = """
    CREATE TABLE rag_access_log (
        id             UUID         NOT NULL DEFAULT gen_random_uuid(),
        user_id        VARCHAR(255) NOT NULL,
        user_role      VARCHAR(50)  NOT NULL,
        org_id         VARCHAR(255),
        query_hash     VARCHAR(64)  NOT NULL,
        chunk_id       VARCHAR(36)  NOT NULL,
        repo           VARCHAR(255) NOT NULL,
        file_path      TEXT         NOT NULL,
        classification VARCHAR(50)  NOT NULL,
        access_granted BOOLEAN      NOT NULL,
        deny_reason    VARCHAR(255),
        session_id     VARCHAR(255),
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        PRIMARY KEY (id)
    ) PARTITION BY HASH (id);
    CREATE INDEX IF NOT EXISTS idx_rag_log_user          ON rag_access_log(user_id);
    CREATE INDEX IF NOT EXISTS idx_rag_log_chunk         ON rag_access_log(chunk_id);
    CREATE INDEX IF NOT EXISTS idx_rag_log_org           ON rag_access_log(org_id);
    CREATE INDEX IF NOT EXISTS idx_rag_log_session       ON rag_access_log(session_id);
    CREATE INDEX IF NOT EXISTS idx_rag_log_created       ON rag_access_log(created_at);
    CREATE INDEX IF NOT EXISTS idx_rag_log_user_created  ON rag_access_log(user_id, created_at DESC);
    """

    _TABLES = [
        ("chat_messages",   _CHAT_PARTITIONED),
        ("thread_messages", _THREAD_PARTITIONED),
        ("model_usages",    _USAGE_PARTITIONED),
        ("rag_access_log",  _RAG_LOG_PARTITIONED),
    ]

    for table_name, create_ddl in _TABLES:
        try:
            with _engine.connect() as conn:
                if not _table_exists(conn, table_name):
                    print(f"  ! Part J: {table_name} does not exist — skipping partition conversion")
                    continue
                if _is_partitioned(conn, table_name):
                    print(f"  ok Part J: {table_name} already partitioned — skip")
                    continue

            # Drop immutability rules on rag_access_log before rename
            if table_name == "rag_access_log":
                try:
                    with _engine.connect() as conn:
                        conn.execute(_text("DROP RULE IF EXISTS rag_access_log_no_delete ON rag_access_log"))
                        conn.execute(_text("DROP RULE IF EXISTS rag_access_log_no_update ON rag_access_log"))
                        conn.commit()
                except Exception:
                    pass

            legacy = f"{table_name}_legacy"
            print(f"  → Part J: converting {table_name} to HASH-partitioned (128 partitions)…")

            with _engine.connect() as conn:
                # Step 1: rename old → legacy
                conn.execute(_text(f"ALTER TABLE {table_name} RENAME TO {legacy}"))
                conn.commit()

            with _engine.connect() as conn:
                # Step 2: create new partitioned table
                conn.execute(_text(create_ddl.strip()))
                conn.commit()

            with _engine.connect() as conn:
                # Step 3: create 128 partitions
                conn.execute(_text(_partition_stmts(table_name)))
                conn.commit()

            with _engine.connect() as conn:
                # Step 4: migrate data.
                #
                # `INSERT INTO t SELECT * FROM legacy` fails with "INSERT has
                # more expressions than target columns" whenever the new
                # partitioned definition and the legacy table differ in column
                # count or order — which is the case for chat_messages,
                # thread_messages and model_usages. Copy the intersection of the
                # two column sets, by name, instead of relying on SELECT *.
                _cols = [r[0] for r in conn.execute(_text(
                    "SELECT a.column_name FROM information_schema.columns a "
                    "WHERE a.table_schema = current_schema() AND a.table_name = :new "
                    "AND EXISTS (SELECT 1 FROM information_schema.columns b "
                    "            WHERE b.table_schema = current_schema() "
                    "              AND b.table_name = :old "
                    "              AND b.column_name = a.column_name) "
                    "ORDER BY a.ordinal_position"
                ), {"new": table_name, "old": legacy}).fetchall()]
                if _cols:
                    _collist = ", ".join(f'"{c}"' for c in _cols)
                    conn.execute(_text(
                        f"INSERT INTO {table_name} ({_collist}) "
                        f"SELECT {_collist} FROM {legacy}"
                    ))
                conn.commit()

            with _engine.connect() as conn:
                # Step 5: drop legacy
                conn.execute(_text(f"DROP TABLE {legacy}"))
                conn.commit()

            # Recreate immutability rules on new rag_access_log
            if table_name == "rag_access_log":
                with _engine.connect() as conn:
                    conn.execute(_text(
                        "CREATE RULE rag_access_log_no_delete AS "
                        "ON DELETE TO rag_access_log DO INSTEAD NOTHING"
                    ))
                    conn.execute(_text(
                        "CREATE RULE rag_access_log_no_update AS "
                        "ON UPDATE TO rag_access_log DO INSTEAD NOTHING"
                    ))
                    conn.commit()

            print(f"  ok Part J: {table_name} → partitioned ({N} partitions)")

        except Exception as exc:
            print(f"  ! Part J: {table_name} conversion failed: {exc}")
            # Attempt rollback: rename legacy back if it exists
            try:
                legacy = f"{table_name}_legacy"
                with _engine.connect() as conn:
                    exists = conn.execute(
                        _text("SELECT 1 FROM information_schema.tables "
                              f"WHERE table_schema='{DB_SCHEMA}' AND table_name=:t"),
                        {"t": legacy}
                    ).fetchone()
                    if exists:
                        # Check if original table also exists (partial failure)
                        orig_exists = conn.execute(
                            _text("SELECT 1 FROM information_schema.tables "
                                  f"WHERE table_schema='{DB_SCHEMA}' AND table_name=:t"),
                            {"t": table_name}
                        ).fetchone()
                        if not orig_exists:
                            conn.execute(_text(f"ALTER TABLE {legacy} RENAME TO {table_name}"))
                            conn.commit()
                            print(f"    → rolled back: {legacy} renamed back to {table_name}")
            except Exception as rb_err:
                print(f"    ! rollback failed: {rb_err}")


# ── Part M: RBAC/ABAC Phase — org_tree + dept_product_mappings + ad_level ────
def _run_part_m():
    _engine = engine
    from sqlalchemy import text as _text

    _PART_M_DDL_STATEMENTS = [
        # org_tree table
        """
CREATE TABLE IF NOT EXISTS org_tree (
    id              SERIAL PRIMARY KEY,
    level           INTEGER NOT NULL,
    node_id         VARCHAR(255),
    parent_id       VARCHAR(255),
    path            TEXT,
    dn              TEXT,
    department      VARCHAR(255),
    description     TEXT,
    direct_reports  INTEGER DEFAULT 0,
    display_name    VARCHAR(255) NOT NULL,
    mail            VARCHAR(255),
    manager         VARCHAR(255),
    mobile          VARCHAR(50),
    title           VARCHAR(255),
    company         VARCHAR(255),
    synced_at       TIMESTAMP NOT NULL DEFAULT NOW()
)
""",
        "CREATE INDEX IF NOT EXISTS idx_org_tree_mail ON org_tree(mail) WHERE mail IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_org_tree_level ON org_tree(level)",
        "CREATE INDEX IF NOT EXISTS idx_org_tree_dept ON org_tree(department) WHERE department IS NOT NULL",
        # dept_product_mappings table
        """
CREATE TABLE IF NOT EXISTS dept_product_mappings (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id   UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    department   VARCHAR(255) NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(product_id, department)
)
""",
        "CREATE INDEX IF NOT EXISTS idx_dpm_department ON dept_product_mappings(department)",
        "CREATE INDEX IF NOT EXISTS idx_dpm_product ON dept_product_mappings(product_id)",
        # ad_level on users
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS ad_level INTEGER NOT NULL DEFAULT 6",
        # visibility + department on agents/skills/workflows
        "ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS visibility VARCHAR(10) NOT NULL DEFAULT 'private'",
        "ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS department VARCHAR(255)",
        "ALTER TABLE skills_pg ADD COLUMN IF NOT EXISTS visibility VARCHAR(10) NOT NULL DEFAULT 'private'",
        "ALTER TABLE skills_pg ADD COLUMN IF NOT EXISTS department VARCHAR(255)",
        "ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS visibility VARCHAR(10) NOT NULL DEFAULT 'private'",
        "ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS department VARCHAR(255)",
        # department on threads and projects
        "ALTER TABLE threads_pg ADD COLUMN IF NOT EXISTS department VARCHAR(255)",
        "ALTER TABLE projects_pg ADD COLUMN IF NOT EXISTS department VARCHAR(255)",
        # Jira / Confluence URL columns on products
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS jira_url TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS confluence_url TEXT",
        # Simplify role — set all non-admin users to 'user'
        "UPDATE users SET role = 'user' WHERE role NOT IN ('admin', 'user')",
        # Drop legacy columns replaced by ad_level + dept_product_mappings
        "ALTER TABLE users DROP COLUMN IF EXISTS band",
        "ALTER TABLE users DROP COLUMN IF EXISTS band_level",
        "ALTER TABLE users DROP COLUMN IF EXISTS is_approver",
        "ALTER TABLE users DROP COLUMN IF EXISTS product_ids",
        # Drop legacy title_band_map table (replaced by org_tree)
        "DROP TABLE IF EXISTS title_band_map",
        # Part N: Product approval workflow
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE'",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS requested_by VARCHAR(255)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(255)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS review_note TEXT",
        "CREATE INDEX IF NOT EXISTS idx_products_status ON products(status)",
        # product_id on threads (already exists but add if missing)
        "ALTER TABLE threads_pg ADD COLUMN IF NOT EXISTS product_id UUID REFERENCES products(id) ON DELETE SET NULL",
        # Department column on document_embeddings (KB dept scoping for RAG)
        "ALTER TABLE document_embeddings ADD COLUMN IF NOT EXISTS department VARCHAR(255)",
        "CREATE INDEX IF NOT EXISTS idx_doc_embed_dept ON document_embeddings(department) WHERE department IS NOT NULL",
        # user_level_overrides — manual ad_level overrides that survive nightly org_tree sync
        """
CREATE TABLE IF NOT EXISTS user_level_overrides (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ad_level_override INTEGER NOT NULL CHECK (ad_level_override BETWEEN 0 AND 6),
    original_level   INTEGER,
    overridden_by    UUID NOT NULL REFERENCES users(id),
    reason           TEXT NOT NULL DEFAULT '',
    expires_at       TIMESTAMP,
    created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    revoked_at       TIMESTAMP,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE
)
""",
        "CREATE INDEX IF NOT EXISTS idx_ulo_user_active ON user_level_overrides(user_id) WHERE is_active = TRUE",
        # Guarded on the role existing — see the note in Part D. Unguarded these
        # failed with `role "ainxt_app" does not exist` and the aborted
        # transaction then killed the two statements after them.
        """
DO $grant$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ainxt_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON user_level_overrides TO ainxt_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON org_tree              TO ainxt_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON dept_product_mappings TO ainxt_app;
  END IF;
END $grant$
""",
    ]

    try:
        with _engine.connect() as conn:
            for stmt in _PART_M_DDL_STATEMENTS:
                try:
                    conn.execute(_text(stmt))
                except Exception as stmt_err:
                    # Non-fatal: log and continue (e.g. if products table doesn't exist yet)
                    print(f"  ! Part M stmt skipped: {stmt_err}")
            conn.commit()
        print("  ok Part M DDL: org_tree, dept_product_mappings, ad_level, visibility/department columns, role simplification")
    except Exception as exc:
        print(f"  ! Part M DDL error: {exc}")


# ── Part N2: Pin chats ────────────────────────────────────────────────────────
def _part_n2_pin_chats():
    from db.database import engine
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text(
                "ALTER TABLE chats ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.commit()
        print("  ok Part N2: chats.is_pinned column added")
    except Exception as exc:
        print(f"  ! Part N2 error: {exc}")


def _part_n3_feedback_context():
    from db.database import engine
    from sqlalchemy import text as _text
    stmts = [
        "ALTER TABLE message_feedback ADD COLUMN IF NOT EXISTS issue             VARCHAR(255)",
        "ALTER TABLE message_feedback ADD COLUMN IF NOT EXISTS sub_issue         VARCHAR(255)",
        "ALTER TABLE message_feedback ADD COLUMN IF NOT EXISTS comment           TEXT",
        "ALTER TABLE message_feedback ADD COLUMN IF NOT EXISTS user_prompt       TEXT",
        "ALTER TABLE message_feedback ADD COLUMN IF NOT EXISTS assistant_summary TEXT",
    ]
    try:
        with engine.connect() as conn:
            for s in stmts:
                conn.execute(_text(s))
            conn.commit()
        print("  ok Part N3: message_feedback context columns added")
    except Exception as exc:
        print(f"  ! Part N3 error: {exc}")


def _part_o_migrate_public_to_ainxt():
    """One-time: copy all data from public schema → ainxt schema.
    Safe to run repeatedly — uses ON CONFLICT DO NOTHING / DO UPDATE.
    Runs the pre-built SQL script if present, then falls back to inline users-only migration."""
    from db.database import engine, DB_SCHEMA
    from sqlalchemy import text as _text
    import os as _os

    # Check if public schema has the users table at all
    try:
        with engine.connect() as conn:
            result = conn.execute(_text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='users'"
            ))
            if result.scalar() == 0:
                print("  · Part O: public schema empty — skip migration")
                return
    except Exception:
        return

    # Try running the full SQL migration script first
    _sql_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "db", "sql", "migrate_public_to_ainxt.sql"
    )
    if _os.path.exists(_sql_path):
        import subprocess as _sp
        _mig_db = _os.getenv("POSTGRES_DB", _CONFIG_POSTGRES_DB)
        result = _sp.run(
            ["psql", "-d", _mig_db, "-f", _sql_path],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print("  ok Part O: public → ainxt full data migration complete")
            return
        else:
            print(f"  ! Part O: SQL script warning (non-fatal): {result.stderr[:200]}")

    # Fallback: migrate users only
    try:
        with engine.connect() as conn:
            conn.execute(_text("""
                INSERT INTO ainxt.users (
                  id, email, name, role, org_id, hashed_password, sso_provider, sso_subject,
                  is_active, last_login_at, account_status, email_verified, created_at,
                  ad_level, ad_username, ad_dn, ad_title, department, manager_dn,
                  gitlab_username, is_security_team, last_ad_sync, default_product_id
                )
                SELECT
                  id, email, name, role, org_id, hashed_password, sso_provider, sso_subject,
                  is_active, last_login_at, account_status, email_verified, created_at,
                  ad_level, ad_username, ad_dn, ad_title, department, manager_dn,
                  gitlab_username, is_security_team, last_ad_sync, default_product_id
                FROM public.users
                ON CONFLICT (email) DO UPDATE SET
                  hashed_password = EXCLUDED.hashed_password,
                  role            = EXCLUDED.role,
                  ad_level        = EXCLUDED.ad_level,
                  department      = EXCLUDED.department,
                  name            = EXCLUDED.name,
                  is_active       = EXCLUDED.is_active,
                  account_status  = EXCLUDED.account_status
            """))
            conn.commit()
        print("  ok Part O: public.users → ainxt.users migrated (fallback)")
    except Exception as exc:
        print(f"  ! Part O: users migration warning: {exc}")


# ── Part P1: 2026-03-26 — Two-level MCP approval ─────────────────────────────
# Reason: Added is_critical flag (set at registration time) and registered_by
# to support two-level approval for critical MCP tools:
#   L1 approver = ad_level <= 3 (Director+)
#   L2 approver = IS/AppSec/InfoSec team (configurable via IS_TEAM_DEPARTMENTS env var)
# Governance state machine: PENDING_APPROVAL → PENDING_L2 → APPROVED (for critical tools)
def _part_p1_mcp_two_level_approval():
    from db.database import engine
    from sqlalchemy import text as _text
    stmts = [
        "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS is_critical   BOOLEAN      NOT NULL DEFAULT FALSE",
        "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS registered_by VARCHAR(255)",
    ]
    try:
        with engine.connect() as conn:
            for s in stmts:
                conn.execute(_text(s))
            conn.commit()
        print("  ok Part P1: mcp_servers.is_critical + registered_by columns added")
    except Exception as exc:
        print(f"  ! Part P1 error: {exc}")


# ── Part P2: 2026-03-26 — Chat message token detail (in_tok, out_tok, latency) ──
# Reason: Inbound/outbound token counts and latency were only tracked in SSE stream,
# not persisted to DB. Adding columns so message history shows full token breakdown.
def _part_p2_message_token_detail():
    from db.database import engine
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            # Check which columns are already present — skip ALTER for those.
            existing = {row[0] for row in conn.execute(_text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'chat_messages' "
                "  AND column_name IN ('in_tok','out_tok','latency')"
            ))}
            needed = [
                ("in_tok",  "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS in_tok  INTEGER"),
                ("out_tok", "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS out_tok INTEGER"),
                ("latency", "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS latency DOUBLE PRECISION"),
            ]
            added = []
            for col, stmt in needed:
                if col not in existing:
                    conn.execute(_text(stmt))
                    added.append(col)
            conn.commit()
        if added:
            print(f"  ok Part P2: chat_messages columns added: {added}")
        else:
            print("  ok Part P2: chat_messages.in_tok/out_tok/latency already present — skipped")
    except Exception as exc:
        print(f"  ! Part P2 error: {exc}")


def _part_p3_knowledge_docs_chunks():
    # 2026-03-26 — Add missing columns to knowledge_docs that exist in ORM but
    # were never applied to the live table: chunks (JSONB), content_hash, chunk_count,
    # file_size, visibility, min_band_level, product_id, department_ids,
    # compliance_pass, approved_by, approved_at, rejection_reason,
    # uploaded_by, uploaded_by_dept, status.
    from db.database import engine
    from sqlalchemy import text as _text
    needed = [
        ("chunks",           "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS chunks           JSONB"),
        ("content_hash",     "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS content_hash     VARCHAR(64)"),
        ("chunk_count",      "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS chunk_count      INTEGER DEFAULT 0"),
        ("file_size",        "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS file_size        INTEGER DEFAULT 0"),
        ("visibility",       "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS visibility       VARCHAR(20) NOT NULL DEFAULT 'PUBLIC'"),
        ("min_band_level",   "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS min_band_level   INTEGER NOT NULL DEFAULT 1"),
        ("product_id",       "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS product_id       UUID"),
        ("department_ids",   "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS department_ids   JSONB NOT NULL DEFAULT '[]'"),
        ("compliance_pass",  "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS compliance_pass  BOOLEAN"),
        ("approved_by",      "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS approved_by      VARCHAR(255)"),
        ("approved_at",      "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS approved_at      TIMESTAMP"),
        ("rejection_reason", "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS rejection_reason TEXT"),
        ("uploaded_by",      "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS uploaded_by      VARCHAR(255)"),
        ("uploaded_by_dept", "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS uploaded_by_dept VARCHAR(255)"),
        ("status",           "ALTER TABLE ainxt.knowledge_docs ADD COLUMN IF NOT EXISTS status           VARCHAR(30) NOT NULL DEFAULT 'PENDING_APPROVAL'"),
    ]
    try:
        with engine.connect() as conn:
            existing = {row[0] for row in conn.execute(_text(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = '{DB_SCHEMA}' AND table_name = 'knowledge_docs'"
            ))}
            added = []
            for col, stmt in needed:
                if col not in existing:
                    conn.execute(_text(stmt))
                    added.append(col)
            conn.commit()
        if added:
            print(f"  ok Part P3: knowledge_docs columns added: {added}")
        else:
            print("  ok Part P3: knowledge_docs columns already present — skipped")
    except Exception as exc:
        print(f"  ! Part P3 error: {exc}")


def _part_p4_mcp_external_servers():
    # 2026-03-30 — External MCP server registry table.
    # Stores connection configs for external MCP servers (GitHub, Slack, Datadog, etc.)
    # that the platform connects to as a client when AiNxt enables external connectivity.
    from db.database import engine
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text(f"""
                CREATE TABLE IF NOT EXISTS ainxt.mcp_external_servers (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name            VARCHAR(100) NOT NULL UNIQUE,
                    transport       VARCHAR(10)  NOT NULL DEFAULT 'stdio',
                    command         VARCHAR(500) NOT NULL DEFAULT '',
                    args            TEXT         NOT NULL DEFAULT '[]',
                    env_vars        TEXT         NOT NULL DEFAULT '{{}}',
                    sse_url         VARCHAR(500) NOT NULL DEFAULT '',
                    sse_headers     TEXT         NOT NULL DEFAULT '{{}}',
                    timeout         FLOAT        NOT NULL DEFAULT 30.0,
                    enabled         BOOLEAN      NOT NULL DEFAULT true,
                    status          VARCHAR(20)  NOT NULL DEFAULT 'disconnected',
                    last_connected_at TIMESTAMP,
                    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW()
                )
            """))
            conn.execute(_text(f"""
                CREATE TABLE IF NOT EXISTS ainxt.tool_audit_log (
                    id          BIGSERIAL PRIMARY KEY,
                    tool_name   VARCHAR(200) NOT NULL,
                    inputs      TEXT,
                    output      TEXT,
                    duration_ms FLOAT,
                    user_id     VARCHAR(200),
                    created_at  TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_mcp_external_servers_name "
                "ON ainxt.mcp_external_servers (name)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_tool_audit_log_tool_created "
                "ON ainxt.tool_audit_log (tool_name, created_at DESC)"
            ))
            conn.commit()
        print("  ok Part P4: mcp_external_servers + tool_audit_log tables created")
    except Exception as exc:
        print(f"  ! Part P4 error: {exc}")


def _part_p5_agent_catalog():
    # 2026-04-03 — Agent catalog: user favorites + agent-scoped KB document links.
    # user_favorites: stores per-user starred agents (entity_type='agent').
    #   entity_type is generic so skills/workflows can be favorited in future.
    # agent_kb_docs: join table linking knowledge_docs rows to an agent by name.
    #   doc_id is plain UUID (no FK) because knowledge_docs may live on a
    #   different DB node; referential integrity is enforced at application layer.
    #   Docs are activated (embedded into pgvector namespace agent_kb:{agent_name})
    #   automatically when the agent is approved via governance.
    from db.database import engine
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS ainxt.user_favorites (
                    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id     UUID        NOT NULL REFERENCES ainxt.users(id) ON DELETE CASCADE,
                    entity_type VARCHAR(50) NOT NULL,
                    entity_id   VARCHAR(255) NOT NULL,
                    created_at  TIMESTAMP   NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_user_favorite UNIQUE (user_id, entity_type, entity_id)
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_user_favorites_user_type "
                "ON ainxt.user_favorites (user_id, entity_type)"
            ))
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS ainxt.agent_kb_docs (
                    id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                    agent_id   VARCHAR(255) NOT NULL,
                    doc_id     UUID         NOT NULL,
                    created_at TIMESTAMP    NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_agent_kb_doc UNIQUE (agent_id, doc_id)
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_agent_kb_docs_agent "
                "ON ainxt.agent_kb_docs (agent_id)"
            ))
            conn.commit()
        print("  ok Part P5: user_favorites + agent_kb_docs tables created")
    except Exception as exc:
        print(f"  ! Part P5 error: {exc}")


def _part_p6_semantic_cache_and_memory():
    # 2026-04-07 — L2 Semantic Answer Cache + L3 Semantic Memory tables.
    #
    # semantic_answer_cache: stores past Q&A pairs with 768-dim embeddings so
    #   semantically similar future questions can be answered without an LLM call.
    #   Uses cosine similarity threshold 0.92 (near-identical intent required).
    #   hit_count / last_used support confidence decay over time.
    #
    # semantic_memory: stores learned patterns from agent runs (debug_pattern,
    #   tool_sequence, design_pattern, failure, tactic, sdlc_learning).
    #   Uses cosine similarity threshold 0.75 (broader pattern reuse).
    #   confidence column enables quality-gated retrieval.
    #
    # Both tables require pgvector extension (already enabled for document_embeddings).
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            # ── L2: Semantic Answer Cache ──────────────────────────────
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS ainxt.semantic_answer_cache (
                    id           UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
                    question     TEXT      NOT NULL,
                    answer       TEXT      NOT NULL,
                    embedding    vector(768),
                    repo_filter  TEXT,
                    user_id      TEXT,
                    confidence   FLOAT     NOT NULL DEFAULT 1.0,
                    hit_count    INT       NOT NULL DEFAULT 0,
                    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
                    last_used    TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_sem_answer_cache_embedding "
                "ON ainxt.semantic_answer_cache USING hnsw (embedding vector_cosine_ops)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_sem_answer_cache_created "
                "ON ainxt.semantic_answer_cache (created_at)"
            ))

            # ── L3: Semantic Memory ────────────────────────────────────
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS ainxt.semantic_memory (
                    id         UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
                    type       TEXT      NOT NULL,
                    summary    TEXT      NOT NULL,
                    content    JSONB     NOT NULL DEFAULT '{}',
                    embedding  vector(768),
                    confidence FLOAT     NOT NULL DEFAULT 0.85,
                    source     TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_sem_memory_embedding "
                "ON ainxt.semantic_memory USING hnsw (embedding vector_cosine_ops)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_sem_memory_type "
                "ON ainxt.semantic_memory (type)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_sem_memory_created "
                "ON ainxt.semantic_memory (created_at)"
            ))

            conn.commit()
        print("  ok Part P6: semantic_answer_cache + semantic_memory tables created")
    except Exception as exc:
        print(f"  ! Part P6 error: {exc}")


def _part_p7_semantic_memory_v2():
    # 2026-04-07 — Semantic memory v2: scope isolation + dedup + hit_count ranking.
    #
    # Adds to ainxt.semantic_memory:
    #   user_id      — who stored this memory (traceability)
    #   scope_type   — "user" | "team" | "org" (default: "org")
    #   scope_id     — user_id / team_name / "global"
    #   hit_count    — reinforcement counter; incremented on dedup conflict
    #   last_used    — timestamp of last retrieval or reinforcement
    #   summary_hash — SHA-256(type::summary) for dedup unique index
    #
    # ON CONFLICT (summary_hash): increments hit_count + raises confidence
    # instead of inserting a duplicate row.
    # Retrieval ranking: similarity*0.5 + confidence*0.3 + hit_count_norm*0.2
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text("ALTER TABLE ainxt.semantic_memory ADD COLUMN IF NOT EXISTS user_id TEXT"))
            conn.execute(_text("ALTER TABLE ainxt.semantic_memory ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'org'"))
            conn.execute(_text("ALTER TABLE ainxt.semantic_memory ADD COLUMN IF NOT EXISTS scope_id TEXT NOT NULL DEFAULT 'global'"))
            conn.execute(_text("ALTER TABLE ainxt.semantic_memory ADD COLUMN IF NOT EXISTS hit_count INT NOT NULL DEFAULT 0"))
            conn.execute(_text("ALTER TABLE ainxt.semantic_memory ADD COLUMN IF NOT EXISTS last_used TIMESTAMP NOT NULL DEFAULT NOW()"))
            conn.execute(_text("ALTER TABLE ainxt.semantic_memory ADD COLUMN IF NOT EXISTS summary_hash CHAR(64)"))

            # Back-fill hash for any existing rows
            conn.execute(_text(
                "UPDATE ainxt.semantic_memory "
                "SET summary_hash = encode(sha256((type || '::' || summary)::bytea), 'hex') "
                "WHERE summary_hash IS NULL"
            ))

            conn.execute(_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sem_memory_dedup "
                "ON ainxt.semantic_memory(summary_hash) "
                "WHERE summary_hash IS NOT NULL"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_sem_memory_hit_count "
                "ON ainxt.semantic_memory(hit_count DESC)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_sem_memory_scope "
                "ON ainxt.semantic_memory(scope_type, scope_id)"
            ))
            conn.commit()
        print("  ok Part P7: semantic_memory v2 — scope + dedup + hit_count")
    except Exception as exc:
        print(f"  ! Part P7 error: {exc}")


def _part_q1_memory_user_scope():
    # 2026-04-08 — Add user_id to conversations and agent_runs for proper session isolation.
    # Without this, memory is keyed only by session_id — guessable UUIDs could leak data.
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text(
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS user_id VARCHAR(255)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_conv_user "
                "ON conversations(user_id, session_id)"
            ))
            conn.execute(_text(
                "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS user_id VARCHAR(255)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_runs_user "
                "ON agent_runs(user_id, created_at)"
            ))
            conn.commit()
        print("  ok Part Q1: user_id on conversations + agent_runs")
    except Exception as exc:
        print(f"  ! Part Q1 error: {exc}")


def _part_q2_model_governance():
    # 2026-04-08 — Department-level model access control.
    # Admins can restrict which models each department can use.
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS dept_model_permissions (
                    id          SERIAL PRIMARY KEY,
                    department  TEXT NOT NULL,
                    model_id    TEXT NOT NULL,
                    allowed     BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by  TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (department, model_id)
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_dmp_dept "
                "ON dept_model_permissions(department)"
            ))
            conn.commit()
        print("  ok Part Q2: dept_model_permissions table")
    except Exception as exc:
        print(f"  ! Part Q2 error: {exc}")


def _part_q3_eval_scores():
    # 2026-04-08 — Per-response eval scores for grounding + completeness tracking.
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS eval_scores (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    request_id      TEXT NOT NULL,
                    user_id         TEXT,
                    department      TEXT,
                    question_hash   TEXT,
                    grounding       FLOAT NOT NULL DEFAULT 0.0,
                    completeness    FLOAT NOT NULL DEFAULT 0.0,
                    chunk_count     INT   NOT NULL DEFAULT 0,
                    has_context     BOOLEAN NOT NULL DEFAULT FALSE,
                    model           TEXT,
                    latency_ms      FLOAT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_eval_created "
                "ON eval_scores(created_at DESC)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_eval_user "
                "ON eval_scores(user_id, created_at DESC)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_eval_dept "
                "ON eval_scores(department, created_at DESC)"
            ))
            conn.commit()
        print("  ok Part Q3: eval_scores table")
    except Exception as exc:
        print(f"  ! Part Q3 error: {exc}")


def _part_r1_client_source_audit():
    """
    2026-04-08 — Client source traceability
    - Adds client_source column to chats (platform|cli|ide-vscode|ide-jetbrains|api)
    - Creates request_audit_log table: one immutable row per /ask call
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            # client_source on chats
            conn.execute(_text(
                "ALTER TABLE chats "
                "ADD COLUMN IF NOT EXISTS client_source VARCHAR(50) NOT NULL DEFAULT 'platform'"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_chats_client_source "
                "ON chats(client_source)"
            ))

            # request_audit_log — immutable, never updated
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS request_audit_log (
                    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    request_id          TEXT        NOT NULL,
                    user_id             TEXT        NOT NULL,
                    email               TEXT,
                    department          TEXT,
                    client_source       VARCHAR(50) NOT NULL DEFAULT 'platform',
                    endpoint            TEXT        NOT NULL,
                    question_hash       TEXT,
                    model_used          TEXT,
                    tokens_in           INT,
                    tokens_out          INT,
                    cost_usd            FLOAT,
                    latency_ms          INT,
                    cache_hit           TEXT,
                    compliance_blocked  BOOLEAN     NOT NULL DEFAULT FALSE,
                    error               TEXT,
                    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_reqaudit_request_id "
                "ON request_audit_log(request_id)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_reqaudit_user "
                "ON request_audit_log(user_id, created_at DESC)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_reqaudit_client "
                "ON request_audit_log(client_source, created_at DESC)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_reqaudit_created "
                "ON request_audit_log(created_at DESC)"
            ))
            conn.commit()
        print("  ok Part R1: client_source + request_audit_log")
    except Exception as exc:
        print(f"  ! Part R1 error: {exc}")


def _part_r2_user_api_keys():
    """
    2026-04-13 — Per-user IDE API keys
    Creates user_api_keys table + indexes.
    Raw key = {username_slug}-{uuid4}; only SHA-256 hash stored.
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS user_api_keys (
                    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id      UUID         NOT NULL REFERENCES users (id) ON DELETE CASCADE,
                    key_prefix   VARCHAR(100) NOT NULL,
                    key_hash     VARCHAR(64)  NOT NULL,
                    label        VARCHAR(255),
                    last_used_at TIMESTAMPTZ,
                    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
                    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    revoked_at   TIMESTAMPTZ,
                    CONSTRAINT uq_user_api_key_hash UNIQUE (key_hash)
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_uak_user   ON user_api_keys(user_id)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_uak_hash   ON user_api_keys(key_hash)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_uak_active ON user_api_keys(user_id, is_active)"
            ))
            conn.commit()
        print("  ok Part R2: user_api_keys table + indexes")
    except Exception as exc:
        print(f"  ! Part R2 error: {exc}")


def _part_s5_user_model_perms():
    """
    2026-04-18 — User-level model access overrides.
    Admins can grant or restrict specific models for individual users within a department,
    taking precedence over department-level dept_model_permissions rules.
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS user_model_permissions (
                    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    department  TEXT NOT NULL,
                    user_id     TEXT NOT NULL,
                    model_id    TEXT NOT NULL,
                    allowed     BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by  TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (user_id, model_id)
                )
            """))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_ump_dept "
                "ON user_model_permissions(department)"
            ))
            conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS idx_ump_user "
                "ON user_model_permissions(user_id)"
            ))
            conn.commit()
        print("  ok Part S5: user_model_permissions table")
    except Exception as exc:
        print(f"  ! Part S5 error: {exc}")


def _part_s6_skill_workflow_created_at():
    """
    2026-04-19 — Add created_at to skills_pg and workflows_pg.
    These tables were created before created_at was included in the DDL,
    so existing rows need the column backfilled via ALTER TABLE.
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text(
                "ALTER TABLE skills_pg ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ))
            conn.execute(_text(
                "ALTER TABLE workflows_pg ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ))
            conn.commit()
        print("  ok Part S6: created_at added to skills_pg and workflows_pg")
    except Exception as exc:
        print(f"  ! Part S6 error: {exc}")


def _part_s7_agent_dynamic_config():
    """
    2026-04-19 — Add kb_namespace + preferred_model to agents_pg.
    kb_namespace scopes the retrieve tool to a domain KB (e.g. docs_kb:hr).
    preferred_model lets each agent prefer a specific LLM tier (auto/claude/gpt/ollama).
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text(
                "ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS kb_namespace VARCHAR(255)"
            ))
            conn.execute(_text(
                "ALTER TABLE agents_pg ADD COLUMN IF NOT EXISTS preferred_model VARCHAR(50)"
            ))
            conn.commit()
        print("  ok Part S7: kb_namespace + preferred_model added to agents_pg")
    except Exception as exc:
        print(f"  ! Part S7 error: {exc}")


def _part_s8_generated_documents():
    """
    2026-04-19 — Generated documents table for chat document generation.
    Stores audit record (metadata + content_md) for every document produced
    via the /docs/generate endpoint.  Binary file lives in /tmp/ainxt_docs/
    with a 24-hour TTL managed by the doc worker.
    """
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS generated_documents (
            id          VARCHAR(36)  PRIMARY KEY,
            job_id      VARCHAR(36)  NOT NULL,
            user_id     VARCHAR(255) NOT NULL,
            chat_id     VARCHAR(36),
            format      VARCHAR(10)  NOT NULL,
            title       VARCHAR(500) NOT NULL,
            filename    VARCHAR(512) NOT NULL,
            file_path   TEXT         NOT NULL,
            content_md  TEXT,
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_gendoc_user    ON generated_documents(user_id);
        CREATE INDEX IF NOT EXISTS idx_gendoc_job     ON generated_documents(job_id);
        CREATE INDEX IF NOT EXISTS idx_gendoc_chat    ON generated_documents(chat_id) WHERE chat_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_gendoc_created ON generated_documents(created_at DESC);
    """, label="Part S8: generated_documents table")


def _part_s9_scaling_indexes():
    """
    2026-04-21 — Scaling audit: add missing indexes.
    PGS01: chat_messages(user_id) — user chat list was doing a full table scan.
    PGS02: document_embeddings(repo, content_hash) — _filter_new_chunks dedup
           query filters on repo + content_hash ANY(:hashes); composite in correct
           column order is faster than the existing (content_hash, repo) unique index.
    """
    from sqlalchemy import text as _text

    # ── PGS01 indexes ─────────────────────────────────────────────────────────
    _pg01_stmts = [
        # Guarded on the column existing. chat_messages has no user_id column
        # (ownership is reached via chat_id -> chats.user_id), so this statement
        # always failed with `column "user_id" does not exist`. Kept guarded
        # rather than deleted: a deployment whose chat_messages does carry
        # user_id still gets the index, and the intended index on the user chat
        # list is on chats(user_id), added below.
        """
        DO $s9$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'chat_messages' AND column_name = 'user_id'
          ) THEN
            CREATE INDEX IF NOT EXISTS idx_chat_messages_user_id
              ON chat_messages (user_id) WHERE user_id IS NOT NULL;
          END IF;
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'chats' AND column_name = 'user_id'
          ) THEN
            CREATE INDEX IF NOT EXISTS idx_chats_user_id
              ON chats (user_id) WHERE user_id IS NOT NULL;
          END IF;
        END $s9$
        """,
    ]
    try:
        with engine.connect() as conn:
            for stmt in _pg01_stmts:
                conn.execute(_text(stmt))
            conn.commit()
        print("  ok Part S9 PGS01: idx_chat_messages_user_id")
    except Exception as exc:
        print(f"  ! Part S9 PGS01 error: {exc}")

    # ── PGS02 indexes ─────────────────────────────────────────────────────────
    # Composite (repo, content_hash) — correct column order for the dedup query:
    #   SELECT content_hash FROM document_embeddings WHERE repo = :repo AND content_hash = ANY(:hashes)
    _pg02_stmts = [
        f"CREATE INDEX IF NOT EXISTS idx_doc_embed_repo_hash ON {DB_SCHEMA}.document_embeddings (repo, content_hash) WHERE content_hash IS NOT NULL",
    ]
    try:
        with vector_engine.connect() as conn:
            for stmt in _pg02_stmts:
                conn.execute(_text(stmt))
            conn.commit()
        print("  ok Part S9 PGS02: idx_doc_embed_repo_hash")
    except Exception as exc:
        print(f"  ! Part S9 PGS02 error: {exc}")


def _part_s10_bm25_gin():
    """
    2026-04-21 — GIN index on document_embeddings.content for BM25 full-text search.

    Without this index, keyword_search() computes to_tsvector('english', content)
    on every row in the repo at query time. For repos with 100k+ vectors this exceeds
    the 60-second statement_timeout, the exception is silently caught, and BM25 always
    returns 0 results — making hybrid retrieval pure pgvector-only.

    With this index, BM25 queries complete in < 50ms regardless of repo size.

    IMPORTANT: statement_timeout is set to 0 for this connection only.
    Building a GIN index on a large table can take 5–30 minutes.
    The connection-level override is safe — it only affects this migration step.
    """
    from sqlalchemy import text as _text

    try:
        with vector_engine.connect() as conn:
            # Disable statement timeout for this connection — GIN index build on a
            # large table takes minutes, far exceeding the default 60s session limit.
            conn.execute(_text("SET statement_timeout = 0"))
            # Drop the old index if it was built on the bare `content` expression —
            # it would error on rows containing null bytes (\x00) and be unusable.
            conn.execute(_text(f"""
                DROP INDEX IF EXISTS {DB_SCHEMA}.idx_doc_embed_content_fts
            """))
            # Rebuild on replace(content, chr(0), '') so null bytes in stored code
            # don't crash to_tsvector(). keyword_search() uses the same expression.
            conn.execute(_text(f"""
                CREATE INDEX IF NOT EXISTS idx_doc_embed_content_fts
                    ON {DB_SCHEMA}.document_embeddings
                    USING GIN (to_tsvector('english', replace(content, chr(0), '')))
            """))
            conn.commit()
        print("  ok Part S10: idx_doc_embed_content_fts (GIN for BM25, null-byte safe)")
    except Exception as exc:
        # Not a failure: Part S11 (_part_s11_bm25_gin_fix, called immediately
        # after this one) rebuilds idx_doc_embed_content_fts without the chr(0)
        # expression that makes this statement fail on real content.
        print(f"  · Part S10 skipped (superseded by Part S11): {str(exc).splitlines()[0][:120]}")
        print(f"    → Run db/sql/prod_catchup_2026_04_21_bm25_gin.sql directly via psql on PGS02")


def _part_s11_bm25_gin_fix():
    """
    2026-04-21 — Fix BM25 GIN index: remove replace(content, chr(0), '').

    chr(0) in a PostgreSQL text expression raises "null character not permitted"
    because PostgreSQL text type cannot store null bytes at all — chr(0) is
    illegal as a text value, so even using it as a replace() argument fails.

    _bulk_upsert._clean() already strips control chars (including \x00) before
    every INSERT, so content is always null-byte-free.  The index and the query
    in hybrid_search.keyword_search() must use the same expression: bare content.
    """
    from sqlalchemy import text as _text

    try:
        with vector_engine.connect() as conn:
            conn.execute(_text("SET statement_timeout = 0"))
            conn.execute(_text(f"DROP INDEX IF EXISTS {DB_SCHEMA}.idx_doc_embed_content_fts"))
            conn.execute(_text(f"""
                CREATE INDEX IF NOT EXISTS idx_doc_embed_content_fts
                    ON {DB_SCHEMA}.document_embeddings
                    USING GIN (to_tsvector('english', content))
            """))
            conn.commit()
        print("  ok Part S11: idx_doc_embed_content_fts rebuilt (bare content, no chr(0))")
    except Exception as exc:
        print(f"  ! Part S11 error (BM25 GIN fix): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_04_21_bm25_gin_fix.sql directly via psql on PGS02")


def _part_s13_skill_behavioral_type():
    """
    2026-04-23 — Add skill_type column to skills_pg.

    'execution' (default) = Python run() code exec'd before LLM call; output injected
    into ## Context section of the prompt.
    'behavioral' = plain-text SOP / domain instructions injected directly into the
    system_prompt section so they carry full instructional authority over the LLM.
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text(
                "ALTER TABLE skills_pg ADD COLUMN IF NOT EXISTS "
                "skill_type VARCHAR(20) NOT NULL DEFAULT 'execution'"
            ))
            conn.commit()
        print("  ok Part S13: skills_pg.skill_type added (execution | behavioral)")
    except Exception as exc:
        print(f"  ! Part S13 error (skill_behavioral_type): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_04_23_skill_behavioral_type.sql directly via psql")


def _part_s12_product_dept_backfill():
    """
    2026-04-22 — Backfill product_id and department from metadata JSONB into first-class columns.

    Prior to this fix, _bulk_upsert() wrote product_id and department only into the
    metadata JSONB column, leaving the indexed first-class columns NULL.  This made
    product-scoped RAG retrieval impossible because hybrid_search.py filters on
    the product_id column, not on metadata->>'product_id'.

    This migration promotes values already stored in JSONB into the proper columns
    so existing indexed repos are immediately searchable by product scope.
    Runs on vector_engine (PGS02 — ainxt_vector).
    """
    from sqlalchemy import text as _text
    try:
        with vector_engine.connect() as conn:
            conn.execute(_text("SET statement_timeout = 0"))
            conn.execute(_text(f"""
                UPDATE {DB_SCHEMA}.document_embeddings
                SET
                    product_id = CAST(NULLIF(metadata->>'product_id', '') AS uuid),
                    department = NULLIF(metadata->>'department', '')
                WHERE
                    (product_id IS NULL OR department IS NULL)
                    AND (
                        metadata->>'product_id' IS NOT NULL
                        OR metadata->>'department' IS NOT NULL
                    )
            """))
            conn.commit()
        print("  ok Part S12: backfilled product_id/department from metadata JSONB into first-class columns")
    except Exception as exc:
        print(f"  ! Part S12 error (product/dept backfill): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_04_22_product_dept_backfill.sql directly via psql on PGS02")


def _part_s14_build_root():
    """
    2026-04-29 — Add build_root to repo_index_status.

    Stores the relative path (from repo root) to the directory containing the
    build system file (pom.xml, package.json, go.mod, etc.).  Needed to support
    monorepos and nested projects where the build file is not at the repo root.

    BuildMetadataExtractor now populates build_root via repo_build_metadata.
    index_worker stores it in repo_index_status for workspace path resolution.
    sdlc_state_machine._execute_tests() uses build_root to cd to the correct dir
    before running mvn/gradle/pytest.

    Default '.' means the repo root (backwards compatible — existing rows untouched).
    """
    _run_ddl("""
        ALTER TABLE repo_index_status
            ADD COLUMN IF NOT EXISTS build_root VARCHAR(1000) DEFAULT '.';
    """)


def _part_s15_projects_default_branch():
    """
    2026-05-03 — Add default_branch to projects_pg.

    Stores the repo's default branch (e.g. "main", "final_setupversion") alongside
    repo_name so SDLC pipeline triggers can pass the correct base_branch without
    requiring a product_repos or index_requests entry.
    """
    _run_ddl("""
        ALTER TABLE projects_pg
            ADD COLUMN IF NOT EXISTS default_branch VARCHAR(255);
    """)


def _part_s16_connector_framework():
    """
    2026-05-03 — Universal Connector Framework.

    Adds two new tables:
      connector_definitions — data-driven connector specs (auth, tools, base_url).
        Any REST API can be added as a connector via DB insert — no code change needed.
        Complex APIs (Microsoft 365, Slack, Gmail) use custom Python adapters.

      user_oauth_tokens — per-user OAuth2 tokens (Fernet-encrypted access + refresh tokens).
        Auto-refreshed by ConnectorEngine before expiry.
        Covers oauth2 | api_key | bearer_token auth types.

    Also seeds built-in connector definitions (Slack, Gmail, Microsoft 365, GitHub, Jira).
    """
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS connector_definitions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name VARCHAR(100) UNIQUE NOT NULL,
            display_name VARCHAR(255) NOT NULL,
            description TEXT,
            icon_url VARCHAR(500),
            category VARCHAR(50) NOT NULL DEFAULT 'custom',
            auth_type VARCHAR(50) NOT NULL DEFAULT 'oauth2',
            auth_config JSONB NOT NULL DEFAULT '{}',
            tools JSONB NOT NULL DEFAULT '[]',
            base_url VARCHAR(500) NOT NULL DEFAULT '',
            has_custom_adapter BOOLEAN NOT NULL DEFAULT FALSE,
            rate_limit_per_min INTEGER NOT NULL DEFAULT 100,
            is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    _run_ddl("""
        CREATE TABLE IF NOT EXISTS user_oauth_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- No FK to users(id): user_id here is the JWT `sub`, a plain string,
            -- while users.id is UUID. A varchar -> uuid FK is rejected by
            -- Postgres, which made this whole CREATE TABLE fail and the two
            -- indexes below fail with "relation user_oauth_tokens does not
            -- exist". Matches the FK-less ainxt.user_tokens table and what
            -- _part_u8_user_oauth_tokens_fix_2026_05_31 already does.
            user_id VARCHAR(255) NOT NULL,
            connector_name VARCHAR(100) NOT NULL,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            expires_at TIMESTAMPTZ,
            scopes TEXT[] DEFAULT '{}',
            metadata JSONB DEFAULT '{}',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, connector_name)
        );
    """)

    _run_ddl("""
        CREATE INDEX IF NOT EXISTS idx_user_oauth_tokens_user_id
            ON user_oauth_tokens(user_id);
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS idx_user_oauth_tokens_connector
            ON user_oauth_tokens(connector_name);
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS idx_connector_definitions_category
            ON connector_definitions(category);
    """)

    print("  ok Part S16: connector_definitions + user_oauth_tokens tables created")

    # Seed built-in connector definitions (idempotent)
    try:
        from connectors.seed import seed_connectors
        seed_connectors()
        print("  ok Part S16: connector seed data inserted")
    except Exception as exc:
        print(f"  ! Part S16 seed warning (non-fatal): {exc}")


def _part_s17_connector_access_control():
    """
    2026-05-03 — Connector access control policy columns.

    Adds two nullable columns to connector_definitions:
      required_ad_level   — NULL means open to all levels.
                            Value N means user.ad_level must be <= N.
                            (0 = top exec only, 6 = everyone)
      allowed_departments — NULL / empty array means open to all departments.
                            Non-empty: user.department must be in this list.

    By default both are NULL (no restriction on existing connectors).
    """
    _run_ddl("""
        ALTER TABLE ainxt.connector_definitions
            ADD COLUMN IF NOT EXISTS required_ad_level INTEGER DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS allowed_departments TEXT[] DEFAULT NULL
    """)
    print("  ok Part S17: required_ad_level + allowed_departments added to connector_definitions")


def _part_s18_user_api_keys():
    """
    2026-05-04 — User API keys table for IDE integrations and service accounts.

    Key format: {username_slug}-{uuid4}  e.g.  kannan-f47ac10b-58a2-...
    Only the SHA-256 hex digest is stored; the plaintext is returned once at
    generation time. Used by IDE Bearer token auth and Presenton service account.
    """
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS user_api_keys (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key_prefix   VARCHAR(100) NOT NULL,
            key_hash     VARCHAR(64)  NOT NULL UNIQUE,
            label        VARCHAR(255),
            last_used_at TIMESTAMPTZ,
            is_active    BOOLEAN NOT NULL DEFAULT TRUE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revoked_at   TIMESTAMPTZ
        )
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS idx_user_api_keys_user_id
            ON user_api_keys(user_id)
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS idx_user_api_keys_key_hash
            ON user_api_keys(key_hash) WHERE is_active = TRUE
    """)
    print("  ok Part S18: user_api_keys table created")


def _part_s19_build_pipeline():
    """
    2026-05-05 — SDLC universal build pipeline tables.

    Three new tables:
      repo_build_metadata   — structured build config extracted from indexed files
                              (pom.xml, package.json, go.mod, .gitlab-ci.yml).
                              One row per repo, updated at index time.
      repo_build_manifests  — resolved + cached BuildManifest used by executor.
                              Includes feedback loop (run_count, success_count, confidence).
      build_runs            — immutable audit log of every compile/test execution.
                              Used for observability, debugging, and LLM context injection.
    """
    # ── repo_build_metadata ───────────────────────────────────────────────────
    from core.config import BUILD_DEPS_COLUMN as _cfg_deps_col
    _deps_col = os.getenv("BUILD_DEPS_COLUMN", _cfg_deps_col)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS repo_build_metadata (
            repo_slug           TEXT        PRIMARY KEY,
            build_tool          TEXT,
            build_file          TEXT,
            language            TEXT,
            language_version    TEXT,
            build_cmd           TEXT,
            test_cmd            TEXT,
            group_id            TEXT,
            artifact_id         TEXT,
            is_multimodule      BOOLEAN     NOT NULL DEFAULT FALSE,
            {_deps_col}           TEXT[]      NOT NULL DEFAULT '{{}}',
            extracted_from      TEXT,
            extraction_method   TEXT,
            confidence          FLOAT       NOT NULL DEFAULT 0.0,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── repo_build_manifests ──────────────────────────────────────────────────
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS repo_build_manifests (
            repo_slug           TEXT        PRIMARY KEY,
            image               TEXT        NOT NULL,
            compile_cmd         TEXT        NOT NULL,
            test_cmd            TEXT        NOT NULL,
            env_vars            JSONB       NOT NULL DEFAULT '{}',
            cache_paths         TEXT[]      NOT NULL DEFAULT '{}',
            timeout_secs        INT         NOT NULL DEFAULT 300,
            detected_by         TEXT        NOT NULL,
            confidence          FLOAT       NOT NULL DEFAULT 0.0,
            invalidated         BOOLEAN     NOT NULL DEFAULT FALSE,
            run_count           INT         NOT NULL DEFAULT 0,
            success_count       INT         NOT NULL DEFAULT 0,
            known_missing_deps  TEXT[]      NOT NULL DEFAULT '{}',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── build_runs ────────────────────────────────────────────────────────────
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS build_runs (
            id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_slug        TEXT        NOT NULL,
            sdlc_run_id      TEXT        NOT NULL,
            phase            TEXT        NOT NULL,
            compile_status   TEXT,
            test_status      TEXT,
            exit_code        INT,
            command          TEXT,
            image            TEXT,
            java_version     TEXT,
            duration_secs    INT,
            output_tail      TEXT,
            error_lines      TEXT[]      NOT NULL DEFAULT '{}',
            failed_tests     TEXT[]      NOT NULL DEFAULT '{}',
            missing_artifact TEXT,
            test_total       INT,
            test_passed      INT,
            test_failed      INT,
            log_path         TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS idx_build_runs_repo_slug
            ON build_runs (repo_slug)
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS idx_build_runs_sdlc_run_id
            ON build_runs (sdlc_run_id)
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS idx_build_runs_created_at
            ON build_runs (created_at DESC)
    """)
    print("  ok Part S19: repo_build_metadata, repo_build_manifests, build_runs tables created")


def _part_s20_workspace_synced_at():
    """
    2026-05-05 — Add workspace_synced_at to repo_index_status.

    workspace_sync_worker.py records the last time a repo workspace was
    git-cloned/reset so eviction logic can prune stale workspaces.
    """
    _run_ddl("""
        ALTER TABLE repo_index_status
            ADD COLUMN IF NOT EXISTS workspace_synced_at TIMESTAMPTZ
    """)
    print("  ok Part S20: workspace_synced_at column added to repo_index_status")


def _part_s21_repo_branch():
    """
    2026-05-05 — Add branch to repo_index_status.

    workspace_sync_worker.py needs to know which git branch to clone/reset
    for each indexed repo so it can call git clone --branch <branch>.
    Populated by index_worker._update_status() when branch is known.
    Default NULL means workspace_sync falls back to "main".
    """
    _run_ddl("""
        ALTER TABLE repo_index_status
            ADD COLUMN IF NOT EXISTS branch VARCHAR(500)
    """)
    print("  ok Part S21: branch column added to repo_index_status")


def _part_t1_model_catalog_2026_05_07():
    """
    2026-05-07 — Model catalog update.

    Adds pricing rows for:
      - gpt-5.4    (replaces gpt-5.2 as the coding/medium tier)
      - gpt-5-5    (new latest/deep tier)
      - claude-opus-4-7  (solution tier, user-selectable)
      - claude-opus-4-6  (user-selectable Opus legacy)

    The gpt-5.2 row is preserved for historical cost attribution.
    ON CONFLICT DO NOTHING — safe to re-run.
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text("""
                INSERT INTO model_rate_table (model_id, provider, input_cost_per_1k, output_cost_per_1k, is_free)
                VALUES
                    ('gpt-5.4',         'openai',    0.005,  0.015,  false),
                    ('gpt-5-5',         'openai',    0.008,  0.024,  false),
                    ('claude-opus-4-7', 'anthropic', 0.015,  0.075,  false),
                    ('claude-opus-4-6', 'anthropic', 0.015,  0.075,  false)
                ON CONFLICT (model_id, effective_from) DO NOTHING
            """))
            conn.commit()
        print("  ok Part T1: model_rate_table seeded with gpt-5.4, gpt-5-5, claude-opus-4-7, claude-opus-4-6")
    except Exception as exc:
        print(f"  ! Part T1 error (model catalog): {exc}")


def _part_t1b_model_catalog_gpt56_2026_07_28():
    """
    2026-07-28 — Model catalog update: GPT-5.6 Tera and Luna.

    Adds pricing rows for:
      - gpt-5.6-terra (high-capacity GPT-5.6 variant — Chat + CLI)
      - gpt-5.6-luna  (efficient GPT-5.6 variant — Chat + CLI)

    Pricing is a placeholder matching gpt-5.5 rates; update when official
    pricing is confirmed.  ON CONFLICT DO NOTHING — safe to re-run.
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text("""
                INSERT INTO model_rate_table (model_id, provider, input_cost_per_1k, output_cost_per_1k, is_free)
                VALUES
                    ('gpt-5.6-terra', 'openai', 0.005, 0.030, false),
                    ('gpt-5.6-luna', 'openai', 0.005, 0.030, false)
                ON CONFLICT (model_id, effective_from) DO NOTHING
            """))
            conn.commit()
        print("  ok Part T1b: model_rate_table seeded with gpt-5.6-terra, gpt-5.6-luna")
    except Exception as exc:
        print(f"  ! Part T1b error (model catalog gpt-5.6): {exc}")


def _part_t2_compress_metrics_2026_05_09():
    """
    2026-05-09 — Context compression telemetry table.

    Stores daily rollup of compression stats per source so the
    /metrics/compression endpoint has persistent history beyond Redis TTL.
    Redis (db=9) is the primary real-time store; this table is the 90-day archive.
    """
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS compress_metrics (
            id          SERIAL PRIMARY KEY,
            metric_date DATE         NOT NULL,
            source      VARCHAR(64)  NOT NULL,
            before_chars BIGINT      NOT NULL DEFAULT 0,
            after_chars  BIGINT      NOT NULL DEFAULT 0,
            call_count   INT         NOT NULL DEFAULT 0,
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (metric_date, source)
        )
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS idx_compress_metrics_date
            ON compress_metrics (metric_date DESC)
    """)
    print("  ok Part T2: compress_metrics table created")


def _part_t3_sdlc_multi_repo_2026_05_19():
    """
    2026-05-19 — SDLC multi-repo support: sibling table for per-repo run state.

    Adds sdlc_run_repos as a child of sdlc_runs. A single-repo run inserts one
    row (kind='primary'); a multi-repo run inserts one row per repo touched.

    sdlc_runs.(repo, pr_url, pr_number) continue to reflect the primary repo —
    UI, audit logs, and Jira webhook acks read them assuming scalar shape.
    Sibling repo MRs and state live in this child table.

    Phase 1 is dormant: the table is created but no pipeline code writes to it
    yet. Phase 2 onward populates rows during preflight.
    """
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS sdlc_run_repos (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id          UUID NOT NULL REFERENCES sdlc_runs(id) ON DELETE CASCADE,
            repo            VARCHAR(500) NOT NULL,
            ref             VARCHAR(255) NOT NULL,
            ref_sha         VARCHAR(64),
            kind            VARCHAR(20)  NOT NULL,
            build_order     INT,
            source          VARCHAR(20),
            pr_url          TEXT,
            pr_number       INT,
            working_branch  VARCHAR(255),
            repo_ctx        JSONB        NOT NULL DEFAULT '{}',
            workspace_path  TEXT,
            state           VARCHAR(50),
            error           TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_sdlc_run_repos_run_repo UNIQUE (run_id, repo)
        )
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS ix_sdlc_run_repos_run_id
            ON sdlc_run_repos (run_id)
    """)
    _run_ddl("""
        CREATE INDEX IF NOT EXISTS ix_sdlc_run_repos_state
            ON sdlc_run_repos (state)
    """)
    print("  ok Part T3: sdlc_run_repos table created")


def _part_s23_drop_file_data():
    """
    2026-05-20 — Drop file_data (BYTEA) column from knowledge_docs.
    Raw file bytes were stored here for pre-approval download. The download
    functionality has been removed entirely. Uses DROP COLUMN IF EXISTS so
    this is safe to run even if the column was never added (e.g. fresh installs).
    """
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text(
                "ALTER TABLE ainxt.knowledge_docs DROP COLUMN IF EXISTS file_data"
            ))
            conn.commit()
        print("  ok Part S23: knowledge_docs.file_data column dropped")
    except Exception as exc:
        print(f"  ! Part S23 error: {exc}")


def _part_t4_cache_token_columns_2026_05_25():
    """
    2026-05-25 — Prompt-caching token columns on model_usages.

    Adds cache_read_tokens and cache_write_tokens to the partitioned
    model_usages table so prompt-cache savings are tracked per request.
    ALTER TABLE on the parent table; partitions inherit the columns automatically.
    """
    _run_ddl(
        "ALTER TABLE model_usages ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT NOT NULL DEFAULT 0",
        "model_usages.cache_read_tokens"
    )
    _run_ddl(
        "ALTER TABLE model_usages ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT NOT NULL DEFAULT 0",
        "model_usages.cache_write_tokens"
    )
    print("  ✓ Part T4: cache_read_tokens / cache_write_tokens added to model_usages")


def _part_x1_source_channel_2026_07_28():
    """
    2026-07-28 — Channel-wise utilization: source_channel on model_usages.

    Adds a first-class source_channel column (CLI / IDE / CHAT / SDLC /
    AGENT-STUDIO / AGENTS) so channel utilization is a simple GROUP BY rather
    than an inference over endpoint/agent_id. ALTER TABLE on the parent table;
    partitions inherit the column automatically.

    Historical rows are backfilled from endpoint (the pre-existing discriminator).
    """
    _run_ddl(
        "ALTER TABLE model_usages ADD COLUMN IF NOT EXISTS source_channel VARCHAR(32)",
        "model_usages.source_channel",
    )
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS idx_model_usages_channel "
        "ON model_usages(source_channel, created_at)",
        "model_usages.idx_source_channel",
    )
    # Backfill historical rows from endpoint. Only touches NULL rows so re-runs
    # never clobber values written by the application at request time.
    _run_ddl(
        """
        UPDATE model_usages SET source_channel =
          CASE
            WHEN endpoint = '/v1/messages'                                 THEN 'CLI'
            WHEN endpoint = '/v1/chat/completions'                         THEN 'IDE'
            WHEN endpoint IN ('/ask', '/ask/cached', '/chat/video-generate') THEN 'CHAT'
            WHEN endpoint = '/sdlc/pipeline'                               THEN 'SDLC'
            WHEN endpoint LIKE 'abstudio.agent.%'                          THEN 'AGENT-STUDIO'
            WHEN endpoint LIKE '/agents/%/run'                             THEN 'AGENTS'
            ELSE NULL
          END
        WHERE source_channel IS NULL
        """,
        "model_usages.source_channel backfill",
    )
    print("  ✓ Part X1: source_channel added + backfilled on model_usages")


def _part_u1_sdlc_stage_artifacts_2026_06_02():
    """
    2026-06-02 — SDLC stage artifacts table.

    Each pipeline stage (CLASSIFYING, ANALYZING, DESIGNING, CODING, SLT,
    REVIEWING, CROSS_MODEL_REVIEW, FIXING, TESTING, COMMITTING) writes one row
    per attempt.  Versioned so re-runs produce a new row rather than
    overwriting — enables diff-based resume and cross-model comparison.

    Also adds suspended_at_stage and created_by columns to sdlc_runs.
    """
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS sdlc_stage_artifacts (
            id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id       UUID         NOT NULL REFERENCES sdlc_runs(id) ON DELETE CASCADE,
            stage        VARCHAR(50)  NOT NULL,
            version      SMALLINT     NOT NULL DEFAULT 1,
            status       VARCHAR(20)  NOT NULL DEFAULT 'PRODUCED',
            payload      JSONB        NOT NULL,
            input_hash   VARCHAR(64)  NOT NULL,
            producer     VARCHAR(128) NOT NULL,
            score        REAL,
            skills_used  JSONB,
            created_by   VARCHAR(128) NOT NULL,
            reason       TEXT,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (run_id, stage, version)
        )
    """, "sdlc_stage_artifacts table")
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS sdlc_stage_artifacts_run_id ON sdlc_stage_artifacts(run_id)",
        "sdlc_stage_artifacts_run_id index"
    )
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS sdlc_stage_artifacts_run_stage ON sdlc_stage_artifacts(run_id, stage)",
        "sdlc_stage_artifacts_run_stage index"
    )
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS suspended_at_stage VARCHAR(50)",
        "sdlc_runs.suspended_at_stage"
    )
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS created_by VARCHAR(128)",
        "sdlc_runs.created_by"
    )
    print("  ✓ Part U1: sdlc_stage_artifacts table + sdlc_runs columns added")


def _part_w_j_sdlc_event_dedupe_2026_06_04():
    """
    2026-06-04 — W-J: De-duplicate the SDLC event stream.

    Problem: ~22/71 event-keys emitted 2–3× per second because both the Kafka
    produce path AND the direct-insert fallback in add_run_event() can persist
    the same logical event.  Also update_run_state() + add_run_event() can both
    append an event row for a single state transition.

    Fix:
    1. New `dedupe_key TEXT` column on sdlc_run_events — stores the event UUID
       minted by add_run_event() for every row.  The Kafka path and the
       direct-insert fallback for the SAME call carry the SAME UUID, so the
       second insert hits the conflict and is silently ignored.  Two genuinely
       distinct same-second events have different UUIDs → both persist.
    2. PARTIAL unique index on (dedupe_key) WHERE dedupe_key IS NOT NULL.
       Partial → existing NULL rows (written before this migration) are
       untouched; no back-fill required.
    3. update_run_state() no longer emits a run_event_appended Kafka message or
       a direct SDLCRunEvent insert; only add_run_event() does.  The consumer's
       run_state_changed handler now ONLY updates the SDLCRun row.

    statement_timeout = 0 is set on the migration connection because unique
    index builds on large tables can take several minutes (consistent with the
    GIN-index precedent in this codebase — see Parts S10/S11).
    """
    # Add dedupe_key column — idempotent
    _run_ddl(
        "ALTER TABLE sdlc_run_events ADD COLUMN IF NOT EXISTS dedupe_key TEXT",
        "sdlc_run_events.dedupe_key column"
    )
    # Build the partial unique index with statement_timeout = 0 (large-table safety)
    from sqlalchemy import text as _text
    try:
        from db.database import engine as _engine
        with _engine.connect() as _conn:
            _conn.execute(_text("SET statement_timeout = 0"))
            _conn.execute(_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sdlc_run_events_dedupe "
                "ON sdlc_run_events (dedupe_key) WHERE dedupe_key IS NOT NULL"
            ))
            _conn.commit()
        print("  ✓ Part W-J: idx_sdlc_run_events_dedupe created (statement_timeout=0)")
    except Exception as _e:
        print(f"  ! Part W-J: index creation error (may already exist): {_e}")
    print("  ✓ Part W-J: sdlc_run_events.dedupe_key + partial unique index added")


def _part_u2_sdlc_hod_budget_2026_06_05():
    """
    2026-06-05 — SDLC HOD budget tracking columns on sdlc_runs.

    Adds five columns that support per-run cost accumulation and HOD cap deduction:
      * total_input_tokens  — cumulative input tokens across all _llm() calls in the run
      * total_output_tokens — cumulative output tokens
      * total_cost_usd      — cumulative USD cost (NUMERIC 12,6 for sub-cent precision)
      * hod_email           — HOD resolved at preflight from ainxt.department_hod_mapping
      * hod_ledger_id       — FK to ainxt.hod_allocation_ledger row written at run end

    All columns are nullable / zero-defaulted so existing rows are unaffected.
    The hod_allocation_ledger gains action='sdlc_run' (no DB-level change needed —
    the action column is VARCHAR(32) with no CHECK constraint).
    """
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS total_input_tokens  INTEGER NOT NULL DEFAULT 0",
        "sdlc_runs.total_input_tokens"
    )
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS total_output_tokens INTEGER NOT NULL DEFAULT 0",
        "sdlc_runs.total_output_tokens"
    )
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS total_cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0",
        "sdlc_runs.total_cost_usd"
    )
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS hod_email VARCHAR(255)",
        "sdlc_runs.hod_email"
    )
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS hod_ledger_id UUID",
        "sdlc_runs.hod_ledger_id"
    )
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS ix_sdlc_runs_hod_email ON sdlc_runs (hod_email) WHERE hod_email IS NOT NULL",
        "ix_sdlc_runs_hod_email"
    )
    print("  ✓ Part U2: sdlc_runs HOD budget columns added")


def _part_v1_sdlc_base_sha_2026_06_17():
    """
    2026-06-17 — Pin each SDLC run to one base commit (workspace consistency).

    Adds sdlc_runs.base_sha: the exact commit the run's workspace is materialized
    against. Captured at the first workspace clone and re-checked-out on every later
    stage / instance that re-materializes the run, so a reused checkout and a fresh
    clone are byte-identical — closing the "different code pulled at different times"
    gap when a run is picked up by a different gateway instance after an HITL gate.
    Nullable, so existing rows and runs with the reuse flag off are unaffected.
    """
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS base_sha VARCHAR(64)",
        "sdlc_runs.base_sha"
    )
    print("  ✓ Part V1: sdlc_runs.base_sha column added")


def _part_v2_sdlc_work_item_2026_06_26():
    """Add canonical Work Item columns produced by TICKET_NORMALIZATION."""
    _run_ddl("""
        ALTER TABLE sdlc_runs
            ADD COLUMN IF NOT EXISTS work_item JSONB,
            ADD COLUMN IF NOT EXISTS normalization_confirmed_at TIMESTAMPTZ
    """)
    print("  ✓ Part V2: sdlc_runs.work_item + normalization_confirmed_at columns added")


def _part_t3_chat_rag_mode_2026_05_21():
    """
    2026-05-21 — chats.rag_mode column.

    Per-chat RAG toggle for the conversational UI. 'off' = generic LLM
    (no KB probe — matches Claude/ChatGPT default), 'auto' = lower-
    threshold KB probe, 'on' = force probe with grounded prompt.
    Old chats default to 'off' so we don't silently flip every existing
    conversation into KB mode.

    Explicitly schema-qualified — the runtime uses {DB_SCHEMA}.chats so
    bare ALTER TABLE could land on public.chats if the connection's
    search_path was overridden by a pooler / parameter-set step.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.chats
            ADD COLUMN IF NOT EXISTS rag_mode VARCHAR(8) NOT NULL DEFAULT 'off'
    """)
    print("  ok Part T3: chats.rag_mode column added (default 'off')")


def _part_t4_user_custom_instructions_2026_05_21():
    """
    2026-05-21 — Custom Instructions on users table.

    Two TEXT blobs (nullable) per user:
      custom_about_user      — "What should AiNxt know about you?"
      custom_response_style  — "How should AiNxt respond?"
    Both are prepended to the system prompt on every /ask call for this
    user, mirroring ChatGPT's Custom Instructions feature.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.users
            ADD COLUMN IF NOT EXISTS custom_about_user     TEXT
    """)
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.users
            ADD COLUMN IF NOT EXISTS custom_response_style TEXT
    """)
    print("  ok Part T4: users.custom_about_user / custom_response_style added")


def _part_t5_message_versions_2026_05_21():
    """
    2026-05-21 — message_versions table for Edit + Branch.

    Each row is a version of a chat_message. Editing a past user message
    creates a new branch (new version with the same root_id, different
    parent_id). Branch traversal: walk versions by parent_id.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.message_versions (
            id              UUID         PRIMARY KEY,
            message_id      UUID         NOT NULL,
            chat_id         UUID         NOT NULL,
            parent_id       UUID,
            root_id         UUID         NOT NULL,
            role            VARCHAR(20)  NOT NULL,
            content         TEXT         NOT NULL,
            version         INTEGER      NOT NULL DEFAULT 1,
            is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_message_versions_message_id
            ON {DB_SCHEMA}.message_versions (message_id)
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_message_versions_chat_id
            ON {DB_SCHEMA}.message_versions (chat_id, created_at DESC)
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_message_versions_root_id
            ON {DB_SCHEMA}.message_versions (root_id)
    """)
    print("  ok Part T5: message_versions table created")


def _part_t6_chat_artifacts_2026_05_21():
    """
    2026-05-21 — chat_artifacts table for Canvas / Artifacts.

    Self-contained HTML/React/SVG/Markdown blocks generated inside chat
    are persisted here and rendered in an iframe-sandboxed side drawer.
    Mirrors Claude Artifacts / ChatGPT Canvas.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.chat_artifacts (
            id            UUID         PRIMARY KEY,
            chat_id       UUID         NOT NULL,
            message_id    UUID,
            title         VARCHAR(200) NOT NULL DEFAULT 'Untitled',
            artifact_type VARCHAR(20)  NOT NULL,     -- html | react | svg | markdown | mermaid | code
            language      VARCHAR(40),               -- for code artifacts
            content       TEXT         NOT NULL,
            version       INTEGER      NOT NULL DEFAULT 1,
            created_by    UUID,
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_chat_artifacts_chat_id
            ON {DB_SCHEMA}.chat_artifacts (chat_id, created_at DESC)
    """)
    print("  ok Part T6: chat_artifacts table created")


def _part_t7_chat_shares_2026_05_21():
    """
    2026-05-21 — chat_shares table for public share links.

    `token` is the URL slug used in /shared/{token}. Read-only snapshot
    semantics: only the messages that exist at share-time are exposed
    (subsequent updates do not propagate unless re-shared).
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.chat_shares (
            token        VARCHAR(64)  PRIMARY KEY,
            chat_id      UUID         NOT NULL,
            owner_id     UUID         NOT NULL,
            snapshot     JSONB        NOT NULL,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            expires_at   TIMESTAMPTZ
        )
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_chat_shares_chat_id
            ON {DB_SCHEMA}.chat_shares (chat_id)
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_chat_shares_owner_id
            ON {DB_SCHEMA}.chat_shares (owner_id)
    """)
    print("  ok Part T7: chat_shares table created")


def _part_t8_prompt_templates_2026_05_21():
    """
    2026-05-21 — prompt_templates table for saved prompts.

    Per-user library of reusable prompt templates. `scope` is 'private'
    (only owner) or 'org' (visible to entire org). The Chat input
    surfaces these via a "/" quick-insert menu.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.prompt_templates (
            id          UUID         PRIMARY KEY,
            user_id     UUID         NOT NULL,
            name        VARCHAR(120) NOT NULL,
            body        TEXT         NOT NULL,
            scope       VARCHAR(10)  NOT NULL DEFAULT 'private',
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_prompt_templates_user_id
            ON {DB_SCHEMA}.prompt_templates (user_id, created_at DESC)
    """)
    print("  ok Part T8: prompt_templates table created")

def _part_t4_cache_token_columns_2026_05_25():
    """
    2026-05-25 — Prompt-caching token columns on model_usages.

    Adds cache_read_tokens and cache_write_tokens to the partitioned
    model_usages table so prompt-cache savings are tracked per request.
    ALTER TABLE on the parent table; partitions inherit the columns automatically.
    """
    _run_ddl(
        "ALTER TABLE model_usages ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT NOT NULL DEFAULT 0",
        "model_usages.cache_read_tokens"
    )
    _run_ddl(
        "ALTER TABLE model_usages ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT NOT NULL DEFAULT 0",
        "model_usages.cache_write_tokens"
    )
    print("  ok Part T4: cache_read_tokens / cache_write_tokens added to model_usages")

def _part_s24_workspace_messages():
    """
    2026-05-20 — workspace_messages: server-side project chat history (Option B).

    Replaces localStorage as the source of truth for workspace (project) chat messages.
    Partition key: project_id — all messages for a project land in the same partition,
    enabling efficient range scans by (project_id, user_id, created_at).

    Design choices:
      - HASH(project_id) with 128 partitions: same strategy as chat_messages/thread_messages.
      - Composite PK (id, project_id): required by Postgres for partitioned tables where
        the partition key must be part of the primary key.
      - user_id isolation enforced at query layer (WHERE user_id = :uid), not via FK,
        to avoid cross-partition FK overhead.
      - Idempotent: IF NOT EXISTS throughout; safe to re-run on any environment.
    """
    from sqlalchemy import text as _text

    N = 128

    def _is_partitioned(conn, table: str) -> bool:
        r = conn.execute(
            _text(
                "SELECT 1 FROM pg_partitioned_table pt "
                "JOIN pg_class c ON c.oid = pt.partrelid "
                "WHERE c.relname = :t"
            ),
            {"t": table},
        ).fetchone()
        return r is not None

    # Check if already partitioned — skip if so
    try:
        with engine.connect() as _conn:
            if _is_partitioned(_conn, "workspace_messages"):
                print("  ok Part S24: workspace_messages already partitioned — skip")
                return
    except Exception as _chk_err:
        print(f"  ! Part S24: partition check failed: {_chk_err}")

    # Create parent partitioned table
    _PARENT_DDL = f"""
CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.workspace_messages (
    id          UUID         NOT NULL DEFAULT gen_random_uuid(),
    project_id  VARCHAR(255) NOT NULL,
    user_id     VARCHAR(255) NOT NULL,
    role        VARCHAR(50)  NOT NULL,
    content     TEXT         NOT NULL,
    model_label VARCHAR(100),
    cost_usd    FLOAT,
    latency     FLOAT,
    in_tok      INTEGER,
    out_tok     INTEGER,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, project_id)
) PARTITION BY HASH (project_id);
"""

    # Create 128 child partitions
    _partition_lines = []
    for i in range(N):
        _partition_lines.append(
            f"CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.workspace_messages_p{i:03d} "
            f"PARTITION OF {DB_SCHEMA}.workspace_messages "
            f"FOR VALUES WITH (MODULUS {N}, REMAINDER {i});"
    )
    _PARTITIONS_DDL = "\n".join(_partition_lines)

    # Indexes — created on the parent; Postgres propagates to all partitions
    _INDEXES_DDL = f"""
CREATE INDEX IF NOT EXISTS idx_wsmsg_project_user_created
    ON {DB_SCHEMA}.workspace_messages (project_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wsmsg_user
    ON {DB_SCHEMA}.workspace_messages (user_id);
CREATE INDEX IF NOT EXISTS idx_wsmsg_project_created
    ON {DB_SCHEMA}.workspace_messages (project_id, created_at DESC);
"""

    try:
        with engine.connect() as conn:
            conn.execute(_text(_PARENT_DDL.strip()))
            conn.commit()
        print("  ok Part S24: workspace_messages parent table created")
    except Exception as exc:
        print(f"  ! Part S24: parent table creation failed: {exc}")
        return

    try:
        with engine.connect() as conn:
            for stmt in [s.strip() for s in _PARTITIONS_DDL.split(";") if s.strip()]:
                conn.execute(_text(stmt))
            conn.commit()
        print(f"  ok Part S24: workspace_messages {N} child partitions created")
    except Exception as exc:
        print(f"  ! Part S24: partition creation failed: {exc}")

    try:
        with engine.connect() as conn:
            for stmt in [s.strip() for s in _INDEXES_DDL.split(";") if s.strip()]:
                conn.execute(_text(stmt))
            conn.commit()
        print("  ok Part S24: workspace_messages indexes created")
    except Exception as exc:
        print(f"  ! Part S24: index creation failed: {exc}")

def _part_s25_workspace_messages_col_rename():
    """
    2026-05-29 — Rename workspace_messages columns to match ORM / migrate.py S24 DDL.

    The table was bootstrapped from db/sql/workspace_db.sql which used different
    column names than the Python backend (models.py + store). This migration
    renames the four divergent columns so INSERTs succeed without UndefinedColumn errors.

      model_used  → model_label
      latency_ms  → latency
      tokens_in   → in_tok
      tokens_out  → out_tok

    Each rename is wrapped in a DO $$ IF EXISTS $$ block for idempotency —
    safe to re-run on any environment (no-op when columns already have the new name).
    """
    from sqlalchemy import text as _text

    _RENAMES = [
        ("model_used", "model_label"),
        ("latency_ms", "latency"),
        ("tokens_in",  "in_tok"),
        ("tokens_out", "out_tok"),
    ]

    _DDL_TEMPLATE = """DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = '{schema}'
          AND table_name   = 'workspace_messages'
          AND column_name  = '{old}'
    ) THEN
        ALTER TABLE {schema}.workspace_messages RENAME COLUMN {old} TO {new};
    END IF;
END$$"""

    try:
        with engine.connect() as conn:
            for old, new in _RENAMES:
                conn.execute(_text(
                    _DDL_TEMPLATE.format(schema=DB_SCHEMA, old=old, new=new)
                ))
            conn.commit()
        print("  ok Part S25: workspace_messages columns renamed to match ORM")
    except Exception as exc:
        print(f"  ! Part S25: workspace_messages column rename failed: {exc}")

def _part_u10_sdlc_stage_artifacts_2026_06_02():
    """
    2026-06-02 — SDLC stage artifacts table.

    Each pipeline stage (CLASSIFYING, ANALYZING, DESIGNING, CODING, SLT,
    REVIEWING, CROSS_MODEL_REVIEW, FIXING, TESTING, COMMITTING) writes one row
    per attempt.  Versioned so re-runs produce a new row rather than
    overwriting — enables diff-based resume and cross-model comparison.

    Also adds suspended_at_stage and created_by columns to sdlc_runs.
    """
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS sdlc_stage_artifacts (
            id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id       UUID         NOT NULL REFERENCES sdlc_runs(id) ON DELETE CASCADE,
            stage        VARCHAR(50)  NOT NULL,
            version      SMALLINT     NOT NULL DEFAULT 1,
            status       VARCHAR(20)  NOT NULL DEFAULT 'PRODUCED',
            payload      JSONB        NOT NULL,
            input_hash   VARCHAR(64)  NOT NULL,
            producer     VARCHAR(128) NOT NULL,
            score        REAL,
            skills_used  JSONB,
            created_by   VARCHAR(128) NOT NULL,
            reason       TEXT,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (run_id, stage, version)
        )
    """, "sdlc_stage_artifacts table")
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS sdlc_stage_artifacts_run_id ON sdlc_stage_artifacts(run_id)",
        "sdlc_stage_artifacts_run_id index"
    )
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS sdlc_stage_artifacts_run_stage ON sdlc_stage_artifacts(run_id, stage)",
        "sdlc_stage_artifacts_run_stage index"
    )
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS suspended_at_stage VARCHAR(50)",
        "sdlc_runs.suspended_at_stage"
    )
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS created_by VARCHAR(128)",
        "sdlc_runs.created_by"
    )
    print("  ok Part U1: sdlc_stage_artifacts table + sdlc_runs columns added")


def _part_u1_kb_scope_metadata_2026_06_02():
    """
    2026-06-02 — KB Phase 1: Spec scope metadata.

    Adds product/domain/version scoping columns to knowledge_docs (PGS01) and
    document_embeddings (PGS02) so every document and every pgvector chunk carries
    a hard scope key (product_id + domain + spec_version).

    Also adds parent_doc_id (version lineage pointer) to knowledge_docs.

    NOTE: The original 2026-06-02 migration also added `git_ref` and
    `object_store_uri` columns for the GitLab mirror and the MinIO/S3 SoR
    respectively. Both have been removed from the architecture — the local
    filesystem at KB_DOC_STORAGE_PATH is the single SoR (see _part_u8 which
    drops git_ref, and _part_u9 which drops object_store_uri on existing
    installs). New installs never create either column.

    These columns enable:
    - Cross-product hallucination prevention (hard pre-ranking scope filter)
    - Version scope cascade (explicit → as-of → active)
    """
    from sqlalchemy import text as _text

    # ── PGS01 — knowledge_docs ────────────────────────────────────────────────
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.knowledge_docs ADD COLUMN IF NOT EXISTS domain           VARCHAR(100);
        ALTER TABLE {DB_SCHEMA}.knowledge_docs ADD COLUMN IF NOT EXISTS spec_version     VARCHAR(50);
        ALTER TABLE {DB_SCHEMA}.knowledge_docs ADD COLUMN IF NOT EXISTS version_date     TIMESTAMPTZ;
        ALTER TABLE {DB_SCHEMA}.knowledge_docs ADD COLUMN IF NOT EXISTS deprecate_prior  BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE {DB_SCHEMA}.knowledge_docs ADD COLUMN IF NOT EXISTS parent_doc_id    UUID
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_knowledge_docs_domain
            ON {DB_SCHEMA}.knowledge_docs (domain) WHERE domain IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_knowledge_docs_spec_version
            ON {DB_SCHEMA}.knowledge_docs (spec_version) WHERE spec_version IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_knowledge_docs_product_domain
            ON {DB_SCHEMA}.knowledge_docs (product_id, domain) WHERE product_id IS NOT NULL
    """)
    print("  ok Part U1: knowledge_docs scope columns + indexes added (PGS01)")

    # ── PGS02 — document_embeddings (vector DB) ───────────────────────────────
    try:
        with vector_engine.connect() as conn:
            for stmt in [
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS domain       VARCHAR(100)",
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS spec_version VARCHAR(50)",
                f"CREATE INDEX IF NOT EXISTS idx_doc_embed_domain ON {DB_SCHEMA}.document_embeddings (domain) WHERE domain IS NOT NULL",
                f"CREATE INDEX IF NOT EXISTS idx_doc_embed_spec_version ON {DB_SCHEMA}.document_embeddings (spec_version) WHERE spec_version IS NOT NULL",
            ]:
                conn.execute(_text(stmt))
            conn.commit()
        print("  ok Part U1: document_embeddings scope columns + indexes added (PGS02)")
    except Exception as exc:
        print(f"  ! Part U1 error (PGS02 document_embeddings): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_06_02.sql directly via psql on PGS02")


def _part_u2_kb_section_chunking_2026_06_02():
    """
    2026-06-02 — KB Phase 2: Section-aware chunking + parent chunk linkage.

    Adds three columns to document_embeddings (PGS02) so KB docs can be chunked
    at heading boundaries and leaves can point to their containing section row:
      - parent_chunk_id   : leaf chunk → parent section row (NULL for parents and code chunks)
      - section_path      : breadcrumb e.g. "1. Intro > 1.2 Scope"
      - is_section_parent : True for the whole-section row, False for sub-chunks

    Enables fast-tier graph parent-expansion in hybrid_retriever:
    retrieve a leaf → fetch its parent → reasoner sees the entire section,
    not just a fragment.
    """
    from sqlalchemy import text as _text
    try:
        with vector_engine.connect() as conn:
            for stmt in [
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS parent_chunk_id   UUID",
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS section_path      TEXT",
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS is_section_parent BOOLEAN NOT NULL DEFAULT FALSE",
                f"CREATE INDEX IF NOT EXISTS idx_doc_embed_parent_chunk_id   ON {DB_SCHEMA}.document_embeddings (parent_chunk_id) WHERE parent_chunk_id IS NOT NULL",
                f"CREATE INDEX IF NOT EXISTS idx_doc_embed_is_section_parent ON {DB_SCHEMA}.document_embeddings (is_section_parent) WHERE is_section_parent = TRUE",
            ]:
                conn.execute(_text(stmt))
            conn.commit()
        print("  ok Part U2: document_embeddings parent_chunk_id/section_path/is_section_parent added (PGS02)")
    except Exception as exc:
        print(f"  ! Part U2 error (PGS02 document_embeddings): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_06_02.sql directly via psql on PGS02")


def _part_u3_kb_version_cascade_2026_06_03():
    """
    2026-06-03 — KB Phase 4: Version scope cascade.

    Adds validity-window columns to knowledge_docs (PGS01) so the version
    resolver can answer 'explicit version → as-of timestamp → active' queries
    without scanning every doc revision.

      valid_from    : when this spec version became authoritative (defaults to version_date)
      valid_to      : when it stopped being authoritative (NULL = still active)

    Plus the dependency/version graph table (kb_edges) on PGS02 so the
    retriever can traverse: product → spec → spec-cross-reference.
    Phase 5 reuses the same table for entity edges (edge_type ENUM-checked).
    """
    from sqlalchemy import text as _text

    # ── PGS01 — knowledge_docs validity window ────────────────────────────────
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.knowledge_docs ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
        ALTER TABLE {DB_SCHEMA}.knowledge_docs ADD COLUMN IF NOT EXISTS valid_to   TIMESTAMPTZ
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_knowledge_docs_valid_from
            ON {DB_SCHEMA}.knowledge_docs (valid_from) WHERE valid_from IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_knowledge_docs_valid_to
            ON {DB_SCHEMA}.knowledge_docs (valid_to)   WHERE valid_to   IS NOT NULL
    """)

    # Back-fill: valid_from = version_date when missing; existing approved docs
    # with no valid_to remain authoritative (= active) until a new version
    # deprecates them via activate_doc.
    _run_ddl(f"""
        UPDATE {DB_SCHEMA}.knowledge_docs
        SET valid_from = version_date
        WHERE valid_from IS NULL AND version_date IS NOT NULL
    """)
    print("  ok Part U3: knowledge_docs valid_from/valid_to added (PGS01)")

    # ── PGS02 — kb_edges (dependency / version / entity graph) ────────────────
    try:
        with vector_engine.connect() as conn:
            conn.execute(_text(f"""
                CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.kb_edges (
                    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    edge_type     VARCHAR(32) NOT NULL,
                    -- one of: structure | dependency | version | entity
                    src_doc_id    UUID,
                    src_chunk_id  UUID,
                    dst_doc_id    UUID,
                    dst_chunk_id  UUID,
                    src_entity_id UUID,
                    dst_entity_id UUID,
                    product_id    UUID,
                    spec_version  VARCHAR(50),
                    props         JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            for stmt in [
                f"CREATE INDEX IF NOT EXISTS idx_kb_edges_type           ON {DB_SCHEMA}.kb_edges (edge_type)",
                f"CREATE INDEX IF NOT EXISTS idx_kb_edges_src_doc        ON {DB_SCHEMA}.kb_edges (src_doc_id) WHERE src_doc_id IS NOT NULL",
                f"CREATE INDEX IF NOT EXISTS idx_kb_edges_dst_doc        ON {DB_SCHEMA}.kb_edges (dst_doc_id) WHERE dst_doc_id IS NOT NULL",
                f"CREATE INDEX IF NOT EXISTS idx_kb_edges_src_entity     ON {DB_SCHEMA}.kb_edges (src_entity_id) WHERE src_entity_id IS NOT NULL",
                f"CREATE INDEX IF NOT EXISTS idx_kb_edges_dst_entity     ON {DB_SCHEMA}.kb_edges (dst_entity_id) WHERE dst_entity_id IS NOT NULL",
                f"CREATE INDEX IF NOT EXISTS idx_kb_edges_product_scope  ON {DB_SCHEMA}.kb_edges (product_id, spec_version) WHERE product_id IS NOT NULL",
            ]:
                conn.execute(_text(stmt))
            conn.commit()
        print("  ok Part U3: kb_edges table created (PGS02)")
    except Exception as exc:
        print(f"  ! Part U3 error (PGS02 kb_edges): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_06_03.sql directly via psql on PGS02")


def _part_u4_kb_entities_2026_06_03():
    """
    2026-06-03 — KB Phase 5: Canonical Entity Registry.

    Stores product-scoped entity nodes so cross-product collisions (UPI in
    Rupay spec vs UPI in UPI spec) are kept separate. Aliases array drives
    the canonical resolver. A null product_id means a curated GLOBAL entity
    (admin allow-list only — extraction never auto-promotes).
    """
    from sqlalchemy import text as _text
    try:
        with vector_engine.connect() as conn:
            conn.execute(_text(f"""
                CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.kb_entities (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    scope_product_id UUID,
                    canonical_name  VARCHAR(255) NOT NULL,
                    kind            VARCHAR(64),
                    aliases         JSONB NOT NULL DEFAULT '[]'::jsonb,
                    props           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    is_global       BOOLEAN NOT NULL DEFAULT FALSE,
                    curated_by      VARCHAR(255),
                    curated_at      TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (scope_product_id, canonical_name)
                )
            """))
            for stmt in [
                f"CREATE INDEX IF NOT EXISTS idx_kb_entities_canon   ON {DB_SCHEMA}.kb_entities (LOWER(canonical_name))",
                f"CREATE INDEX IF NOT EXISTS idx_kb_entities_scope   ON {DB_SCHEMA}.kb_entities (scope_product_id) WHERE scope_product_id IS NOT NULL",
                f"CREATE INDEX IF NOT EXISTS idx_kb_entities_global  ON {DB_SCHEMA}.kb_entities (is_global) WHERE is_global = TRUE",
                f"CREATE INDEX IF NOT EXISTS idx_kb_entities_aliases ON {DB_SCHEMA}.kb_entities USING GIN(aliases)",
            ]:
                conn.execute(_text(stmt))
            conn.commit()
        print("  ok Part U4: kb_entities table created (PGS02)")
    except Exception as exc:
        print(f"  ! Part U4 error (PGS02 kb_entities): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_06_03.sql directly via psql on PGS02")


def _part_u5_kb_chunk_status_2026_06_03():
    """
    2026-06-03 — KB Phase 1 closure: chunk-level active-version filter.

    Adds `status` column to document_embeddings (PGS02), stamped to 'ACTIVE'
    by docs_store.activate_doc on insert and flipped to 'DEPRECATED' by the
    deprecate_prior branch of activate_doc when a newer spec version supersedes
    it. The Fast tier (hybrid_search) and Coverage tier filter on
    `status='ACTIVE'` so deprecation is instant — no re-index, no delete, no
    stale chunks bleeding into retrieval.

    Default 'ACTIVE' guarantees existing rows behave as before until activate_doc
    explicitly deprecates them.
    """
    from sqlalchemy import text as _text
    try:
        with vector_engine.connect() as conn:
            for stmt in [
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE'",
                f"CREATE INDEX IF NOT EXISTS idx_doc_embed_status ON {DB_SCHEMA}.document_embeddings (status) WHERE status = 'ACTIVE'",
            ]:
                conn.execute(_text(stmt))
            conn.commit()
        print("  ok Part U5: document_embeddings.status added (PGS02)")
    except Exception as exc:
        print(f"  ! Part U5 error (PGS02 document_embeddings): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_06_03_chunk_status.sql directly via psql on PGS02")


def _part_u7_chat_message_coverage_trace_2026_06_03():
    """
    2026-06-03 — Phase 3 transparency persistence.

    Adds nullable JSONB `coverage_trace` to chat_messages so the Coverage
    badge in the UI survives a page reload. Stored verbatim from the SSE
    __meta__ frame's coverage_trace dict (gate decision + sections examined
    + badge text). NULL on user messages and on assistant messages produced
    before Phase 1 scope wiring landed.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.chat_messages
            ADD COLUMN IF NOT EXISTS coverage_trace JSONB
    """, label="Part U7: chat_messages.coverage_trace added (PGS01)")


def _part_u8_drop_knowledge_docs_git_ref_2026_06_03():
    """
    2026-06-03 — Storage architecture cleanup: drop knowledge_docs.git_ref.

    The original Phase 1 design (kn_rewrite.md, pre-cleanup) included a
    mandatory GitLab/GitHub mirror for approved spec docs and stored its
    file URL in `git_ref`. That mirror has been removed from the design:
    the local filesystem at KB_DOC_STORAGE_PATH is the single system of
    record for the full doc body. No SCM mirror participates in
    query-time retrieval.

    This migration drops the now-unused column from existing installs.
    New installs never write it (removed from _part_u1 and the SQL mirror).
    DROP COLUMN IF EXISTS is safe on databases that never created it.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.knowledge_docs DROP COLUMN IF EXISTS git_ref
    """, label="Part U8: knowledge_docs.git_ref dropped (no SCM mirror)")


def _part_u9_drop_knowledge_docs_object_store_uri_2026_06_03():
    """
    2026-06-03 — Storage architecture cleanup: drop knowledge_docs.object_store_uri.

    The original Phase 3 design stored an opaque URI (MinIO/S3 key, or later
    the `fs:<abs_path>` string) on each doc row so kb_doc_cache could load
    the body. The architecture now stores all bodies at the deterministic
    path KB_DOC_STORAGE_PATH/<doc_id>.md — the URI is implicit from the
    doc_id, so the column is dead weight.

    kb_doc_cache.warm now opens `<KB_DOC_STORAGE_PATH>/<doc_id>.md` directly.
    docs_store.activate_doc stops writing the column. This migration drops
    it from existing installs. DROP COLUMN IF EXISTS is safe on databases
    that never created it.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.knowledge_docs DROP COLUMN IF EXISTS object_store_uri
    """, label="Part U9: knowledge_docs.object_store_uri dropped (path is implicit)")


def _part_u6_chat_scope_2026_06_03():
    """
    2026-06-03 — Per-chat KB scope persistence.

    Adds four nullable columns to chats so the user's selected
    {product_id, domain, spec_version, kb_doc_id} is server-side state
    (not client-spoofable per kn_rewrite.md §7). The /ask gateway SELECTs
    these on every request and injects them into _user_ctx['scope_filter']
    (+ _user_ctx['kb_doc_id']), so the existing Phase 1–5 retrieval machinery
    fires on every chat-time RAG request — no longer stranded.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.chats ADD COLUMN IF NOT EXISTS product_id   UUID;
        ALTER TABLE {DB_SCHEMA}.chats ADD COLUMN IF NOT EXISTS domain       VARCHAR(100);
        ALTER TABLE {DB_SCHEMA}.chats ADD COLUMN IF NOT EXISTS spec_version VARCHAR(50);
        ALTER TABLE {DB_SCHEMA}.chats ADD COLUMN IF NOT EXISTS kb_doc_id    UUID
    """, label="Part U6: chats KB scope columns added (PGS01)")
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_chats_product_id   ON {DB_SCHEMA}.chats (product_id)   WHERE product_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_chats_spec_version ON {DB_SCHEMA}.chats (spec_version) WHERE spec_version IS NOT NULL
    """, label="Part U6: chats KB scope indexes")


def _part_w_j_sdlc_event_dedupe_2026_06_04():
    """
    2026-06-04 — W-J: De-duplicate the SDLC event stream.

    Problem: ~22/71 event-keys emitted 2–3× per second because both the Kafka
    produce path AND the direct-insert fallback in add_run_event() can persist
    the same logical event.  Also update_run_state() + add_run_event() can both
    append an event row for a single state transition.

    Fix:
    1. New `dedupe_key TEXT` column on sdlc_run_events — stores the event UUID
       minted by add_run_event() for every row.  The Kafka path and the
       direct-insert fallback for the SAME call carry the SAME UUID, so the
       second insert hits the conflict and is silently ignored.  Two genuinely
       distinct same-second events have different UUIDs → both persist.
    2. PARTIAL unique index on (dedupe_key) WHERE dedupe_key IS NOT NULL.
       Partial → existing NULL rows (written before this migration) are
       untouched; no back-fill required.
    3. update_run_state() no longer emits a run_event_appended Kafka message or
       a direct SDLCRunEvent insert; only add_run_event() does.  The consumer's
       run_state_changed handler now ONLY updates the SDLCRun row.

    statement_timeout = 0 is set on the migration connection because unique
    index builds on large tables can take several minutes (consistent with the
    GIN-index precedent in this codebase — see Parts S10/S11).
    """
    # Add dedupe_key column — idempotent
    _run_ddl(
        "ALTER TABLE sdlc_run_events ADD COLUMN IF NOT EXISTS dedupe_key TEXT",
        "sdlc_run_events.dedupe_key column"
    )
    # Build the partial unique index with statement_timeout = 0 (large-table safety)
    from sqlalchemy import text as _text
    try:
        from db.database import engine as _engine
        with _engine.connect() as _conn:
            _conn.execute(_text("SET statement_timeout = 0"))
            _conn.execute(_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sdlc_run_events_dedupe "
                "ON sdlc_run_events (dedupe_key) WHERE dedupe_key IS NOT NULL"
            ))
            _conn.commit()
        print("  ok Part W-J: idx_sdlc_run_events_dedupe created (statement_timeout=0)")
    except Exception as _e:
        print(f"  ! Part W-J: index creation error (may already exist): {_e}")
    print("  ok Part W-J: sdlc_run_events.dedupe_key + partial unique index added")


def _part_s26_rag_mode_isolation():
    """
    2026-06-04 — Context isolation: add rag_mode columns to prevent cross-mode
    data leakage between Generic and KB/codebase chats.

    Affected tables:
      - chat_messages            → rag_mode VARCHAR(8)
      - conversations            → rag_mode VARCHAR(8), source_repo TEXT
      - ainxt.semantic_answer_cache → rag_mode VARCHAR(8)
      - ainxt.semantic_memory       → rag_mode VARCHAR(8), source_repo TEXT

    All columns are nullable so existing rows (NULL = untagged historical data)
    are excluded from Generic reads (fail-closed).

    CHECK constraints enforce the enum; partial indexes keep Generic read
    filtering cheap.

    Each table is handled in its own transaction so that a missing pgvector
    table (local dev without vector extension) does not abort the migration
    for tables that DO exist.
    """
    from sqlalchemy import text as _text

    _rag_enum = "('off','auto','on','voice','cli','unknown')"

    # ── Table definitions: (table, columns_sql[], indexes_sql[], constraint_name) ──
    _tables = [
        {
            "label": "chat_messages",
            "columns": [
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS rag_mode VARCHAR(8)",
            ],
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_chatmsg_rag_mode "
                "ON chat_messages(rag_mode) WHERE rag_mode = 'off'",
            ],
            "constraint": "chk_chatmsg_rag_mode",
        },
        {
            "label": "conversations",
            "columns": [
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS rag_mode VARCHAR(8)",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS source_repo TEXT",
            ],
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_conv_rag_mode "
                "ON conversations(rag_mode) WHERE rag_mode = 'off'",
            ],
            "constraint": "chk_conv_rag_mode",
        },
        {
            "label": "ainxt.semantic_answer_cache",
            "columns": [
                "ALTER TABLE ainxt.semantic_answer_cache ADD COLUMN IF NOT EXISTS rag_mode VARCHAR(8)",
            ],
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_sem_cache_rag_mode "
                "ON ainxt.semantic_answer_cache(rag_mode) WHERE rag_mode = 'off'",
            ],
            "constraint": "chk_sem_cache_rag_mode",
        },
        {
            "label": "ainxt.semantic_memory",
            "columns": [
                "ALTER TABLE ainxt.semantic_memory ADD COLUMN IF NOT EXISTS rag_mode VARCHAR(8)",
                "ALTER TABLE ainxt.semantic_memory ADD COLUMN IF NOT EXISTS source_repo TEXT",
            ],
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_sem_memory_rag_mode "
                "ON ainxt.semantic_memory(rag_mode) WHERE rag_mode = 'off'",
            ],
            "constraint": "chk_sem_memory_rag_mode",
        },
    ]

    ok_count = 0
    for tdef in _tables:
        _lbl = tdef["label"]
        try:
            with engine.connect() as conn:
                for col_sql in tdef["columns"]:
                    conn.execute(_text(col_sql))
                for idx_sql in tdef["indexes"]:
                    conn.execute(_text(idx_sql))
                # CHECK constraint (idempotent via pg_constraint lookup)
                _cname = tdef["constraint"]
                conn.execute(_text(f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = '{_cname}'
                        ) THEN
                            ALTER TABLE {_lbl}
                            ADD CONSTRAINT {_cname}
                            CHECK (rag_mode IS NULL OR rag_mode IN {_rag_enum});
                        END IF;
                    END$$;
                """))
                conn.commit()
            ok_count += 1
            print(f"  + Part S26: {_lbl} -- rag_mode column + index + CHECK added")
        except Exception as exc:
            print(f"  ! Part S26: {_lbl} skipped -- {exc}")

    if ok_count == len(_tables):
        print(f"  + Part S26: all rag_mode context isolation columns ready")
    else:
        print(f"  ~ Part S26: {ok_count}/{len(_tables)} tables migrated (missing tables likely need pgvector)")


# ============================================================
# Part U11 — chunk hierarchy metadata (PGS02 document_embeddings)
# ============================================================
# Source: AiNxt_Retrieval_Discussion_Summary.docx §8 (Structured Document
# Storage) + §10 (example graph: source_type drives lineage typing).
#
# Adds four nullable columns to document_embeddings so citations can show
# {doc_name, section_name, page_number, source_type} without a cross-DB
# join into PGS01.knowledge_docs on every chunk lookup.
#
#   doc_name     : denormalised KnowledgeDocument.name — fast citation render.
#   section_name : leaf heading (last segment of section_path) — UI badge.
#   page_number  : 1-based page index from the PDF parser — NULL for
#                  non-paginated sources (Markdown / Word / code).
#   source_type  : BRD / FSD / TPMC_DECISION / RBI_CIRCULAR / ARCHITECTURE
#                  / SPEC / OTHER (CHECK-enforced enum; denormalised from
#                  the parent knowledge_docs.source_type added in U13).
#
# A partial index on (source_type) lets Fast-tier filtering ("only FSDs")
# stay cheap. NULL passes the CHECK so existing rows are untouched —
# nullable everywhere = fail-soft on read.
def _part_u11_kb_chunk_hierarchy_2026_06_08():
    """
    2026-06-08 — Chunk hierarchy metadata for citation rendering.

    Adds page_number / section_name / doc_name / source_type to
    document_embeddings (PGS02). All nullable; partial index on source_type;
    CHECK enforces the source_type enum.
    """
    from sqlalchemy import text as _text
    try:
        with vector_engine.connect() as conn:
            for stmt in [
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS page_number  INTEGER",
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS section_name TEXT",
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS doc_name     TEXT",
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings ADD COLUMN IF NOT EXISTS source_type  VARCHAR(32)",
                f"CREATE INDEX IF NOT EXISTS idx_doc_embed_source_type "
                f"ON {DB_SCHEMA}.document_embeddings (source_type) WHERE source_type IS NOT NULL",
            ]:
                conn.execute(_text(stmt))
            # CHECK constraint (idempotent via pg_constraint lookup — Postgres
            # has no `ADD CONSTRAINT IF NOT EXISTS`).
            conn.execute(_text(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'chk_doc_embed_source_type'
                    ) THEN
                        ALTER TABLE {DB_SCHEMA}.document_embeddings
                        ADD CONSTRAINT chk_doc_embed_source_type
                        CHECK (source_type IS NULL OR source_type IN
                            ('BRD','FSD','TPMC_DECISION','RBI_CIRCULAR',
                             'ARCHITECTURE','SPEC','OTHER'));
                    END IF;
                END$$;
            """))
            conn.commit()
        print("  ok Part U11: document_embeddings page_number/section_name/doc_name/source_type added (PGS02)")
    except Exception as exc:
        print(f"  ! Part U11 error (PGS02 document_embeddings): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_06_08_retrieval_v2.sql directly via psql on PGS02")


# ============================================================
# Part U12 — BM25 exact-term tsvector (PGS02 document_embeddings)
# ============================================================
# Source: AiNxt_Retrieval_Discussion_Summary.docx §4 ("Best when users
# search for exact terms such as FASTag, NACH, settlement exception,
# RBI circular references … Preserves exact terminology").
#
# Today keyword_search() in models/hybrid_search.py uses an english-config
# tsvector + a Python pre-tokeniser that requires a leading letter and
# strips punctuation — so "RBI/2024-25/12" is reduced to just "RBI" and
# quoted phrases lose their phrase intent.
#
# Fix: a GENERATED stored tsvector column using the 'simple' config (no
# stemming, no stop-word removal) so identifier strings survive verbatim.
# Generated columns are maintained by Postgres automatically — app code
# never writes them. A GIN index on the column keeps the BM25 path fast.
#
# statement_timeout = 0 mirrors the W-J / S10 / S11 precedent for large-
# table GIN builds (the existing english GIN index was built the same way).
def _part_u12_kb_simple_tsv_2026_06_08():
    """
    2026-06-08 — Parallel 'simple' tsvector for exact-term BM25.

    Adds content_simple_tsv (GENERATED ALWAYS AS to_tsvector('simple',
    coalesce(content, '')) STORED) + GIN index. Identifier-shaped queries
    and quoted phrases route through this column via phraseto_tsquery
    in models/hybrid_search.py:keyword_search().
    """
    from sqlalchemy import text as _text
    try:
        with vector_engine.connect() as conn:
            _conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            _conn.execute(_text("SET statement_timeout = 0"))
            _conn.execute(_text(
                f"ALTER TABLE {DB_SCHEMA}.document_embeddings "
                f"ADD COLUMN IF NOT EXISTS content_simple_tsv tsvector "
                f"GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED"
            ))
            _conn.execute(_text(
                f"CREATE INDEX IF NOT EXISTS idx_doc_embed_simple_tsv "
                f"ON {DB_SCHEMA}.document_embeddings USING GIN (content_simple_tsv)"
            ))
        print("  ok Part U12: document_embeddings.content_simple_tsv + GIN index added (PGS02)")
    except Exception as exc:
        print(f"  ! Part U12 error (PGS02 document_embeddings): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_06_08_retrieval_v2.sql directly via psql on PGS02")


# ============================================================
# Part U13 — knowledge_docs source_type + original_ext (PGS01)
# ============================================================
# Source: AiNxt_Retrieval_Discussion_Summary.docx §8 (source_type as
# hierarchy metadata) + §2 (retain originals for complex layouts).
#
# source_type   : one row per doc (denormalised into document_embeddings
#                 by U11). Captured at upload via the new dropdown in
#                 KnowledgeBase.jsx.
# original_ext  : lowercased extension of the uploaded file (e.g. 'pdf').
#                 Path stays implicit — the original binary is written to
#                 KB_DOC_STORAGE_PATH/<doc_id>.<ext> alongside the .md.
#                 Citation footer shows a "Open original" link derived
#                 from this value.
def _part_u13_kdocs_source_type_2026_06_08():
    """
    2026-06-08 — knowledge_docs source_type + original_ext.

    Adds two nullable columns + a partial index on source_type + a CHECK
    that mirrors the document_embeddings.source_type enum from U11.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.knowledge_docs ADD COLUMN IF NOT EXISTS source_type  VARCHAR(32);
        ALTER TABLE {DB_SCHEMA}.knowledge_docs ADD COLUMN IF NOT EXISTS original_ext VARCHAR(16)
    """, label="Part U13: knowledge_docs source_type + original_ext columns")
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_kdocs_source_type
            ON {DB_SCHEMA}.knowledge_docs (source_type) WHERE source_type IS NOT NULL
    """, label="Part U13: knowledge_docs source_type partial index")
    # CHECK constraint (idempotent via pg_constraint lookup).
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'chk_kdocs_source_type'
                    ) THEN
                        ALTER TABLE {DB_SCHEMA}.knowledge_docs
                        ADD CONSTRAINT chk_kdocs_source_type
                        CHECK (source_type IS NULL OR source_type IN
                            ('BRD','FSD','TPMC_DECISION','RBI_CIRCULAR',
                             'ARCHITECTURE','SPEC','OTHER'));
                    END IF;
                END$$;
            """))
            conn.commit()
        print("  ok Part U13: knowledge_docs source_type CHECK constraint added (PGS01)")
    except Exception as exc:
        print(f"  ! Part U13 CHECK constraint error: {exc}")


# ============================================================
# Part U14 — kb_edges relation index + CHECK (PGS02 kb_edges)
# ============================================================
# Source: AiNxt_Retrieval_Discussion_Summary.docx §9–§11 (graph traversal
# for impact/lineage). The example chain from §10 — TPMC_DECISION
# approves SPEC → SPEC implements RBI_CIRCULAR → CIRCULAR referenced_by
# BRD/FSD — needs a way to record the relation sub-kind on each edge.
#
# Decision (locked): schemaless via props.relation JSONB. This matches the
# existing convention — workers/kb_entity_worker.py already writes
# props={"relation": "co_occurs"} for entity co-occurrence edges. Adding
# a typed column would force a backfill of those rows; staying schemaless
# keeps the migration shape-stable.
#
# Adds:
#   - A functional partial index on (edge_type, (props->>'relation'))
#     filtered WHERE props ? 'relation' — covers all current and future
#     traversal queries (neighbors_for_doc / has_dependency_leak).
#   - A CHECK constraint pinning the allowed relation strings so a typo
#     in the regex extractor can't silently widen the namespace.
def _part_u14_kb_edges_relation_2026_06_08():
    """
    2026-06-08 — kb_edges relation index + CHECK.

    No new column. Adds a functional index on (edge_type, props->>'relation')
    + a CHECK pinning the allowed relation sub-kinds (co_occurs / approves
    / approved_by / implements / implemented_by / governs / governed_by /
    references / referenced_by / supersedes / superseded_by).
    """
    from sqlalchemy import text as _text
    try:
        with vector_engine.connect() as conn:
            conn.execute(_text(
                f"CREATE INDEX IF NOT EXISTS idx_kb_edges_relation "
                f"ON {DB_SCHEMA}.kb_edges (edge_type, (props->>'relation')) "
                f"WHERE props ? 'relation'"
            ))
            # CHECK constraint (idempotent via pg_constraint lookup).
            conn.execute(_text(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'chk_kb_edges_relation'
                    ) THEN
                        ALTER TABLE {DB_SCHEMA}.kb_edges
                        ADD CONSTRAINT chk_kb_edges_relation
                        CHECK (
                            (props->>'relation') IS NULL OR
                            (props->>'relation') IN
                            ('co_occurs','approves','approved_by',
                             'implements','implemented_by',
                             'governs','governed_by',
                             'references','referenced_by',
                             'supersedes','superseded_by')
                        );
                    END IF;
                END$$;
            """))
            conn.commit()
        print("  ok Part U14: kb_edges relation index + CHECK added (PGS02)")
    except Exception as exc:
        print(f"  ! Part U14 error (PGS02 kb_edges): {exc}")
        print(f"    → Run db/sql/prod_catchup_2026_06_08_retrieval_v2.sql directly via psql on PGS02")


def _part_v1_managed_endpoints_2026_06_11():
    """
    2026-06-11 — Managed Endpoints feature.

    Creates the managed_endpoints table (via create_all) and ensures all
    columns exist for environments that already have the table from a
    previous partial migration.

    Each row represents a named OpenAI-compatible proxy endpoint with:
      - a slug (URL path segment)
      - an allowed_models JSONB list
      - a single bearer token stored as SHA-256 hash (token_hash)
      - a display-only token_prefix (first 20 chars of raw token)
    """
    _run_ddl(
        """
        ALTER TABLE managed_endpoints
            ADD COLUMN IF NOT EXISTS description  TEXT,
            ADD COLUMN IF NOT EXISTS token_prefix VARCHAR(30),
            ADD COLUMN IF NOT EXISTS token_hash   VARCHAR(64),
            ADD COLUMN IF NOT EXISTS require_auth BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS enabled      BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS created_by   VARCHAR(255),
            ADD COLUMN IF NOT EXISTS org_id       VARCHAR(255) NOT NULL DEFAULT 'default'
        """,
        "Part V1: managed_endpoints columns",
    )
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS ix_managed_endpoints_slug ON managed_endpoints(slug)",
        "Part V1: managed_endpoints slug index",
    )
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS ix_managed_endpoints_org_id ON managed_endpoints(org_id)",
        "Part V1: managed_endpoints org_id index",
    )


def _part_v3_managed_endpoints_apikey_2026_06_12():
    """
    2026-06-12 — Managed Endpoints: platform-generated API key + use_env_key toggle.

    Changes:
      - ADD api_key_id UUID FK → user_api_keys(id) ON DELETE SET NULL
        The platform-generated API key for caller auth lives in user_api_keys
        (same table as CLI keys). This column references that row.
      - ADD use_env_key BOOLEAN NOT NULL DEFAULT FALSE
        When TRUE  → platform forwards os.getenv(env_key_name) to LiteLLM
        When FALSE → platform forwards global LOCAL_LLM_API_KEY to LiteLLM
      - ALTER env_key_name to be nullable (only required when use_env_key=TRUE)

    Idempotent: IF NOT EXISTS / IF EXISTS guards throughout.
    """
    _run_ddl(
        """
        ALTER TABLE managed_endpoints
            ADD COLUMN IF NOT EXISTS api_key_id  UUID REFERENCES user_api_keys(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS use_env_key BOOLEAN NOT NULL DEFAULT FALSE
        """,
        "Part V3: managed_endpoints api_key_id FK + use_env_key toggle",
    )
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS ix_managed_endpoints_api_key_id ON managed_endpoints(api_key_id)",
        "Part V3: managed_endpoints api_key_id index",
    )
    # env_key_name is now optional (only needed when use_env_key=TRUE)
    _run_ddl(
        "ALTER TABLE managed_endpoints ALTER COLUMN env_key_name DROP NOT NULL",
        "Part V3: managed_endpoints env_key_name nullable",
    )

def _part_v4_managed_endpoints_2026_06_22():
    """
    2026-06-22 — Endpoint usage tracking + tool calls toggle.

    Changes:
      - managed_endpoints.tool_calls_enabled BOOLEAN NOT NULL DEFAULT TRUE
      - managed_endpoints.system_user_id UUID FK → users(id) ON DELETE SET NULL
      - users.failed_login_attempts INTEGER NOT NULL DEFAULT 0
      - users.locked_until TIMESTAMPTZ
      - chats.endpoint_slug VARCHAR(100)
    """
    _run_ddl(
        """
        ALTER TABLE managed_endpoints
            ADD COLUMN IF NOT EXISTS tool_calls_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS system_user_id UUID REFERENCES users(id) ON DELETE SET NULL
        """,
        "Part V4: managed_endpoints tool_calls_enabled + system_user_id",
    )
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS ix_managed_endpoints_system_user_id ON managed_endpoints(system_user_id)",
        "Part V4: managed_endpoints system_user_id index",
    )
    _run_ddl(
        "ALTER TABLE chats ADD COLUMN IF NOT EXISTS endpoint_slug VARCHAR(100)",
        "Part V4: chats endpoint_slug",
    )
    _run_ddl(
        "CREATE INDEX IF NOT EXISTS ix_chats_endpoint_slug ON chats(endpoint_slug)",
        "Part V4: chats endpoint_slug index",
    )


def _part_v2_managed_endpoints_revision_2026_06_11():
    """
    2026-06-11 — Managed Endpoints schema revision.

    Replaces the token-based auth model with LiteLLM-key-based auth:
      - ADD env_key_name VARCHAR(255)  — name of the .env var holding the LiteLLM API key
      - DROP allowed_models            — models now fetched live from LiteLLM per-key
      - DROP token_prefix              — no platform-generated tokens
      - DROP token_hash                — no platform-generated tokens
      - DROP require_auth              — auth is always required (key from env)

    Idempotent: uses IF NOT EXISTS / IF EXISTS guards throughout.
    """
    # Add new column
    _run_ddl(
        """
        ALTER TABLE managed_endpoints
            ADD COLUMN IF NOT EXISTS env_key_name VARCHAR(255)
        """,
        "Part V2: managed_endpoints ADD env_key_name",
    )
    # Set a placeholder for any existing rows that predate this migration
    _run_ddl(
        """
        UPDATE managed_endpoints
           SET env_key_name = 'LITELLM_API_KEY'
         WHERE env_key_name IS NULL
        """,
        "Part V2: managed_endpoints backfill env_key_name",
    )
    # Make it NOT NULL now that all rows have a value
    _run_ddl(
        """
        ALTER TABLE managed_endpoints
            ALTER COLUMN env_key_name SET NOT NULL
        """,
        "Part V2: managed_endpoints env_key_name NOT NULL",
    )
    # Drop obsolete columns (IF EXISTS — safe on fresh installs that never had them)
    for col in ("allowed_models", "token_prefix", "token_hash", "require_auth"):
        _run_ddl(
            f"ALTER TABLE managed_endpoints DROP COLUMN IF EXISTS {col}",
            f"Part V2: managed_endpoints DROP {col}",
        )

def _part_v1_sdlc_base_sha_2026_06_17():
    """
    2026-06-17 — Pin each SDLC run to one base commit (workspace consistency).

    Adds sdlc_runs.base_sha: the exact commit the run's workspace is materialized
    against. Captured at the first workspace clone and re-checked-out on every later
    stage / instance that re-materializes the run, so a reused checkout and a fresh
    clone are byte-identical — closing the "different code pulled at different times"
    gap when a run is picked up by a different gateway instance after an HITL gate.
    Nullable, so existing rows and runs with the reuse flag off are unaffected.
    """
    _run_ddl(
        "ALTER TABLE sdlc_runs ADD COLUMN IF NOT EXISTS base_sha VARCHAR(64)",
        "sdlc_runs.base_sha"
    )
    print("  ok Part V1: sdlc_runs.base_sha column added")


def _part_w1_scanned_pdf_flag_2026_06_17():
    """
    2026-06-17 — Scanned PDF upload support.

    Adds is_scanned_pdf BOOLEAN column to knowledge_docs (PGS01).

    Context:
      Image-only (scanned) PDFs have no embedded text, so the legacy parser
      returns 0 chars at upload time.  Previously this caused the upload to be
      rejected with "No text could be extracted from file".

      The new column flags these documents so:
        - upload_doc() bypasses the empty-text rejection gate
        - activate_doc() knows to run deferred compliance on the PaddleOCR-
          extracted text (instead of skipping compliance entirely)

    Idempotent: IF NOT EXISTS guard.
    """
    _run_ddl(
        "ALTER TABLE knowledge_docs "
        "ADD COLUMN IF NOT EXISTS is_scanned_pdf BOOLEAN NOT NULL DEFAULT FALSE",
        "Part W1: knowledge_docs is_scanned_pdf flag (scanned PDF upload support)",
    )


def _part_w1_scanned_pdf_flag_2026_06_17():
    """
    2026-06-17 — Scanned PDF upload support.

    Adds is_scanned_pdf BOOLEAN column to knowledge_docs (PGS01).

    Context:
      Image-only (scanned) PDFs have no embedded text, so the legacy parser
      returns 0 chars at upload time.  Previously this caused the upload to be
      rejected with "No text could be extracted from file".

      The new column flags these documents so:
        - upload_doc() bypasses the empty-text rejection gate
        - activate_doc() knows to run deferred compliance on the PaddleOCR-
          extracted text (instead of skipping compliance entirely)

    Idempotent: IF NOT EXISTS guard.
    """
    _run_ddl(
        "ALTER TABLE knowledge_docs "
        "ADD COLUMN IF NOT EXISTS is_scanned_pdf BOOLEAN NOT NULL DEFAULT FALSE",
        "Part W1: knowledge_docs is_scanned_pdf flag (scanned PDF upload support)",
    )


def _part_u1_cowork_parity_2026_05_30():
    """
    2026-05-30 — Cowork → Claude Cowork parity backend tables.

    Role specialists (cowork_roles), per-user personalization (cowork_user_memory),
    scheduled/recurring tasks (cowork_scheduled_tasks + cowork_task_runs), and
    enterprise per-tool connector controls (cowork_connector_policy + role grants).
    Canonical schema reconciled across the scheduler/worker/router. All ids are
    TEXT (uuid strings); no cross-schema FKs (created_by/user_id are plain TEXT).
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_roles (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name                VARCHAR(255) NOT NULL,
            description         TEXT,
            system_prompt       TEXT NOT NULL,
            allowed_connectors  JSONB NOT NULL DEFAULT '[]'::jsonb,
            skill_names         JSONB NOT NULL DEFAULT '[]'::jsonb,
            subagent_allowlist  JSONB NOT NULL DEFAULT '[]'::jsonb,
            department          VARCHAR(255),
            visibility          VARCHAR(10) NOT NULL DEFAULT 'private',
            created_by          VARCHAR(255),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_cowork_roles_name_dept UNIQUE (name, department)
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS ix_cowork_roles_dept ON {DB_SCHEMA}.cowork_roles (department)")

    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_user_memory (
            user_id     TEXT PRIMARY KEY,
            prefs       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_scheduled_tasks (
            id                TEXT PRIMARY KEY,
            user_id           TEXT NOT NULL,
            role              TEXT,
            prompt            TEXT NOT NULL,
            cron              TEXT NOT NULL,
            connectors        JSONB NOT NULL DEFAULT '[]'::jsonb,
            status            TEXT NOT NULL DEFAULT 'active',
            approved_action   JSONB,
            action_allowlist  JSONB NOT NULL DEFAULT '[]'::jsonb,
            next_run          TIMESTAMPTZ,
            last_run          TIMESTAMPTZ,
            last_run_status   TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_sched_due ON {DB_SCHEMA}.cowork_scheduled_tasks (next_run) WHERE status = 'active'")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_sched_user ON {DB_SCHEMA}.cowork_scheduled_tasks (user_id)")

    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_task_runs (
            id          TEXT PRIMARY KEY,
            task_id     TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            status      TEXT NOT NULL,
            output      TEXT,
            error       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_task_runs_task ON {DB_SCHEMA}.cowork_task_runs (task_id, created_at DESC)")

    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_connector_policy (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            department  VARCHAR(255),
            connector   VARCHAR(100) NOT NULL,
            tool        VARCHAR(255) NOT NULL DEFAULT '*',
            allow       BOOLEAN NOT NULL DEFAULT TRUE,
            created_by  VARCHAR(255),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (department, connector, tool)
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_conn_policy_lookup ON {DB_SCHEMA}.cowork_connector_policy (connector, tool)")

    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.role_connector_grants (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            role            VARCHAR(50) NOT NULL,
            connector_name  VARCHAR(100) NOT NULL,
            created_by      VARCHAR(255),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (role, connector_name)
        )
    """)
    print("  ok Part U1: Cowork parity tables created (roles, memory, scheduled_tasks, task_runs, connector_policy, role_grants)")


def _part_u2_cowork_usage_2026_05_30():
    """
    2026-05-30 — Cowork usage analytics + group spend + computer-use audit.

    cowork_usage         — per-turn cost/token rows (user/dept/role) for analytics
                           + group spend-limit enforcement.
    cowork_spend_limits  — per-department monthly USD cap.
    cowork_computer_use_audit — every native/browser computer-use action (P4),
                           values never stored; only the event + allow/block + redaction.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_usage (
            id           BIGSERIAL PRIMARY KEY,
            user_id      TEXT NOT NULL,
            department   TEXT,
            role         TEXT,
            surface      TEXT NOT NULL DEFAULT 'cowork',   -- cowork | office | scheduled
            model        TEXT,
            cost_usd     DOUBLE PRECISION NOT NULL DEFAULT 0,
            input_tokens BIGINT NOT NULL DEFAULT 0,
            output_tokens BIGINT NOT NULL DEFAULT 0,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_usage_user ON {DB_SCHEMA}.cowork_usage (user_id, created_at DESC)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_usage_dept ON {DB_SCHEMA}.cowork_usage (department, created_at DESC)")

    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_spend_limits (
            department      VARCHAR(255) PRIMARY KEY,
            monthly_usd     DOUBLE PRECISION NOT NULL DEFAULT 0,   -- 0 = unlimited
            updated_by      VARCHAR(255),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_computer_use_audit (
            id            BIGSERIAL PRIMARY KEY,
            user_id       TEXT NOT NULL,
            department    TEXT,
            session_id    TEXT NOT NULL,
            action        TEXT NOT NULL,
            target        TEXT,                              -- app bundle-id / host (never values)
            allowed       BOOLEAN NOT NULL DEFAULT FALSE,
            block_reason  TEXT,
            findings_count INTEGER NOT NULL DEFAULT 0,
            redacted      BOOLEAN NOT NULL DEFAULT FALSE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_cu_audit_user ON {DB_SCHEMA}.cowork_computer_use_audit (user_id, created_at DESC)")
    print("  ok Part U2: Cowork usage + spend limits + computer-use audit tables created")


def _part_u3_cowork_enterprise_2026_05_30():
    """
    2026-05-30 — Cowork enterprise parity tail:

    1. Marketplace publishing — cowork_roles gets a governance status
       (DRAFT → PUBLISHED) + published_at/published_by, so a role/plugin is
       authored privately then explicitly PUBLISHED to the org marketplace
       (visibility=public is flipped on publish). Idempotent ADD COLUMN.
    2. Dispatch (mobile → desktop) — cowork_dispatch lets a user create a task
       from any client (web/mobile) that the user's running DESKTOP claims and
       executes locally (computer-use/connectors live there). Single-claim via
       an atomic UPDATE … WHERE status='queued'. Results posted back for the
       originating client to read.

    Append-only; both backed by plain TEXT user ids (no cross-schema FKs).
    """
    # 1. Marketplace publishing on roles -------------------------------------
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_roles ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'DRAFT'")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_roles ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_roles ADD COLUMN IF NOT EXISTS published_by VARCHAR(255)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_roles_status ON {DB_SCHEMA}.cowork_roles (status)")

    # 2. Mobile → desktop dispatch -------------------------------------------
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_dispatch (
            id           TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL,
            prompt       TEXT NOT NULL,
            role         TEXT,
            project      JSONB,
            origin       TEXT NOT NULL DEFAULT 'mobile',   -- mobile | web | api
            status       TEXT NOT NULL DEFAULT 'queued',    -- queued | claimed | done | failed | cancelled
            claimed_by   TEXT,                              -- desktop instance id
            result       TEXT,
            error        TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            claimed_at   TIMESTAMPTZ,
            finished_at  TIMESTAMPTZ
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_dispatch_pending ON {DB_SCHEMA}.cowork_dispatch (user_id, created_at) WHERE status = 'queued'")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_dispatch_user ON {DB_SCHEMA}.cowork_dispatch (user_id, created_at DESC)")
    print("  ok Part U3: Cowork marketplace publishing + mobile→desktop dispatch")


def _part_u4_cowork_usage_scale_2026_05_30():
    """
    2026-05-30 — cowork_usage scaling (feedback_scale_2k_users).

    The analytics endpoints used to SUM() over the full raw cowork_usage table,
    which slows as it grows under 2k users. Fix:

    1. cowork_usage_daily — a pre-aggregated DAILY rollup (day × dept × user ×
       surface). The usage sink upserts into it per turn; analytics + spend-limit
       checks read THIS (tiny, indexed), never scanning the raw rows.
    2. BRIN index on raw cowork_usage(created_at) — append-only + time-ordered, so
       BRIN gives cheap time-range scans for detail/audit at ~zero storage cost.

    NOTE: monthly RANGE partitioning of the raw table is provided SEPARATELY as a
    maintenance-window script (db/sql/maint_cowork_usage_partition_2026_05_30.sql)
    — converting a live table to partitioned requires a rewrite, so it is NOT run
    silently on startup. The rollup is what removes the hot-path scan.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_usage_daily (
            day         DATE   NOT NULL,
            department  TEXT   NOT NULL DEFAULT '',
            user_id     TEXT   NOT NULL DEFAULT '',
            surface     TEXT   NOT NULL DEFAULT 'cowork',
            cost_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
            tokens      BIGINT NOT NULL DEFAULT 0,
            turns       BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (day, department, user_id, surface)
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_usage_daily_dept ON {DB_SCHEMA}.cowork_usage_daily (department, day)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_usage_daily_user ON {DB_SCHEMA}.cowork_usage_daily (user_id, day)")
    # BRIN on the raw append-only table — tiny, ideal for created_at range scans.
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_usage_created_brin ON {DB_SCHEMA}.cowork_usage USING BRIN (created_at)")
    print("  ok Part U4: cowork_usage_daily rollup + BRIN on raw (analytics off the hot path)")


def _part_u5_cowork_projects_2026_05_31():
    """
    2026-05-31 — Server-persisted Cowork PROJECTS + project-linked schedules.

    Projects were client-only (renderer localStorage), so they weren't durable,
    weren't multi-device, and schedules couldn't reference them. Move projects to
    Postgres and link scheduled tasks to a project so a user can see all schedules
    for a project. Also store the schedule's TIMEZONE so cron fires in the user's
    local time (was implicitly UTC).
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_projects (
            id            TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL,
            name          TEXT NOT NULL,
            instructions  TEXT NOT NULL DEFAULT '',
            memory        TEXT NOT NULL DEFAULT '',
            folder        TEXT,
            department    TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_projects_user ON {DB_SCHEMA}.cowork_projects (user_id, updated_at DESC)")
    # Link schedules to a project (nullable — general schedules have no project).
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS project_id TEXT")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_sched_project ON {DB_SCHEMA}.cowork_scheduled_tasks (project_id)")
    # Schedule timezone (IANA name, e.g. 'Asia/Kolkata') so cron fires in local time.
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS tz TEXT NOT NULL DEFAULT 'UTC'")
    # Fix: approved_action was originally BOOLEAN (unusable by the worker which expects a JSONB
    # object describing the connector action to execute).  Migrate to JSONB so the worker can
    # read {connector, tool, params} directly.  Existing FALSE rows become NULL (no approval).
    # ADD COLUMN IF NOT EXISTS is idempotent; the USING cast only runs when the column already
    # exists as BOOLEAN — safe to re-run on a fresh DB where the column is already JSONB.
    _run_ddl(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = '{DB_SCHEMA}'
                  AND table_name   = 'cowork_scheduled_tasks'
                  AND column_name  = 'approved_action'
                  AND data_type    = 'boolean'
            ) THEN
                -- Drop the boolean default first; it cannot be auto-cast to JSONB
                ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks
                    ALTER COLUMN approved_action DROP DEFAULT;
                ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks
                    ALTER COLUMN approved_action TYPE JSONB
                    USING NULL;
            END IF;
        END $$;
    """)

    # 2026-08-10 — Outlook-style Recurrence (mirror of
    # db/sql/prod_catchup_2026_08_10_cowork_scheduler_recurrence.sql).
    #
    # See routers/cowork_tasks_router.py (schema fields) and
    # workers/cowork_scheduler.py (range/max/interval gates + `completed`
    # transition) for how these columns are consumed. All idempotent so a
    # fresh install and an upgraded DB end up in identical shape.
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS starts_at       TIMESTAMPTZ")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS ends_at         TIMESTAMPTZ")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS max_runs        INTEGER")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS runs_count      INTEGER NOT NULL DEFAULT 0")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS interval_weeks  SMALLINT")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS interval_months SMALLINT")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS recurrence      JSONB")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD COLUMN IF NOT EXISTS summary         TEXT")
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_cowork_sched_range_end "
        f"ON {DB_SCHEMA}.cowork_scheduled_tasks (ends_at) WHERE status = 'active'"
    )

    print("  ok Part U5: cowork_projects (server) + schedule project_id + tz + approved_action JSONB fix + recurrence columns")


def _part_u6_cowork_conversations_2026_05_31():
    """
    2026-05-31 — Server-persisted Cowork CONVERSATIONS (chat history).

    Conversation history was the last thing living in renderer localStorage. Move
    it to Postgres so it's durable + multi-device + project-scoped — completing the
    "no work in localStorage; everything in the DB" rule. Messages are JSONB (the
    renderer's message-block array). Scoped per-user; optionally linked to a project.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cowork_conversations (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            project_id  TEXT,
            folder      TEXT,
            title       TEXT NOT NULL DEFAULT 'Conversation',
            messages    JSONB NOT NULL DEFAULT '[]'::jsonb,
            resume_id   TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # resume_id (2026-07-09): the agent's real session_id, used to --resume the
    # Buddy session so an in-progress task/thread continues across navigation and
    # app restarts instead of spawning a fresh empty agent. Idempotent for
    # already-created tables.
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.cowork_conversations ADD COLUMN IF NOT EXISTS resume_id TEXT")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_conv_user ON {DB_SCHEMA}.cowork_conversations (user_id, updated_at DESC)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_cowork_conv_project ON {DB_SCHEMA}.cowork_conversations (user_id, project_id)")
    print("  ok Part U6: cowork_conversations (server-persisted chat history)")


def _part_u7_cowork_hash_partitions_2026_05_31():
    """
    2026-05-31 — HASH(user_id) partition cowork_conversations + cowork_scheduled_tasks.

    Both are per-user hot tables. HASH-by-user partitioning (16-way) prunes per-user
    queries to one partition and lets autovacuum/maintenance run per-partition as the
    tables grow to 10k+ users — aligning with the "partition hot tables" scale rule.
    Cheap to do now while the tables are tiny. The PRIMARY KEY must include the
    partition key, so it becomes (id, user_id) (id stays effectively unique).

    Idempotent: each block no-ops if the table is already partitioned. Recreate via
    LIKE (copies columns+defaults), add PK, create 16 partitions, copy rows, drop old,
    re-create indexes.
    """
    _run_ddl(f"""
        DO $$
        DECLARE is_part boolean; i int;
        BEGIN
            SELECT (c.relkind = 'p') INTO is_part FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = '{DB_SCHEMA}' AND c.relname = 'cowork_conversations';
            IF COALESCE(is_part, false) THEN RAISE NOTICE 'cowork_conversations already partitioned'; RETURN; END IF;
            ALTER TABLE {DB_SCHEMA}.cowork_conversations RENAME TO cowork_conversations_old;
            EXECUTE 'CREATE TABLE {DB_SCHEMA}.cowork_conversations (LIKE {DB_SCHEMA}.cowork_conversations_old INCLUDING DEFAULTS) PARTITION BY HASH (user_id)';
            EXECUTE 'ALTER TABLE {DB_SCHEMA}.cowork_conversations ADD PRIMARY KEY (id, user_id)';
            FOR i IN 0..15 LOOP
                EXECUTE format('CREATE TABLE {DB_SCHEMA}.cowork_conversations_p%s PARTITION OF {DB_SCHEMA}.cowork_conversations FOR VALUES WITH (MODULUS 16, REMAINDER %s)', i, i);
            END LOOP;
            EXECUTE 'INSERT INTO {DB_SCHEMA}.cowork_conversations SELECT * FROM {DB_SCHEMA}.cowork_conversations_old';
            EXECUTE 'DROP TABLE {DB_SCHEMA}.cowork_conversations_old';
            EXECUTE 'CREATE INDEX idx_cowork_conv_user ON {DB_SCHEMA}.cowork_conversations (user_id, updated_at DESC)';
            EXECUTE 'CREATE INDEX idx_cowork_conv_project ON {DB_SCHEMA}.cowork_conversations (user_id, project_id)';
        END $$;
    """)
    _run_ddl(f"""
        DO $$
        DECLARE is_part boolean; i int;
        BEGIN
            SELECT (c.relkind = 'p') INTO is_part FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = '{DB_SCHEMA}' AND c.relname = 'cowork_scheduled_tasks';
            IF COALESCE(is_part, false) THEN RAISE NOTICE 'cowork_scheduled_tasks already partitioned'; RETURN; END IF;
            ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks RENAME TO cowork_scheduled_tasks_old;
            EXECUTE 'CREATE TABLE {DB_SCHEMA}.cowork_scheduled_tasks (LIKE {DB_SCHEMA}.cowork_scheduled_tasks_old INCLUDING DEFAULTS) PARTITION BY HASH (user_id)';
            EXECUTE 'ALTER TABLE {DB_SCHEMA}.cowork_scheduled_tasks ADD PRIMARY KEY (id, user_id)';
            FOR i IN 0..15 LOOP
                EXECUTE format('CREATE TABLE {DB_SCHEMA}.cowork_scheduled_tasks_p%s PARTITION OF {DB_SCHEMA}.cowork_scheduled_tasks FOR VALUES WITH (MODULUS 16, REMAINDER %s)', i, i);
            END LOOP;
            EXECUTE 'INSERT INTO {DB_SCHEMA}.cowork_scheduled_tasks SELECT * FROM {DB_SCHEMA}.cowork_scheduled_tasks_old';
            EXECUTE 'DROP TABLE {DB_SCHEMA}.cowork_scheduled_tasks_old';
            EXECUTE 'CREATE INDEX idx_cowork_sched_due ON {DB_SCHEMA}.cowork_scheduled_tasks (next_run) WHERE status = ''active''';
            EXECUTE 'CREATE INDEX idx_cowork_sched_user ON {DB_SCHEMA}.cowork_scheduled_tasks (user_id)';
            EXECUTE 'CREATE INDEX idx_cowork_sched_project ON {DB_SCHEMA}.cowork_scheduled_tasks (project_id)';
        END $$;
    """)
    print("  ok Part U7: HASH(user_id) partitions on cowork_conversations + cowork_scheduled_tasks (16-way)")


def _part_u8_user_oauth_tokens_fix_2026_05_31():
    """2026-05-31 — Create ainxt.user_oauth_tokens (corrected).

    Part S16 (_part_s16_connector_framework) declared this table with
    `user_id VARCHAR(255) NOT NULL REFERENCES users(id)`, but users.id is UUID —
    a varchar→uuid FK Postgres rejects, so that CREATE TABLE failed silently
    (swallowed by _run_ddl) and the table was never created, while the sibling
    connector_definitions table in the same part succeeded. The connector OAuth
    callback (_store_token) therefore failed for every real connection.

    Fix: create the table WITHOUT the broken FK (user_id is the JWT `sub`, a plain
    string — matching the FK-less ainxt.user_tokens table). Idempotent via
    IF NOT EXISTS, so it no-ops where it already exists.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.user_oauth_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id VARCHAR(255) NOT NULL,
            connector_name VARCHAR(100) NOT NULL,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            expires_at TIMESTAMPTZ,
            scopes TEXT[] DEFAULT '{{}}',
            metadata JSONB DEFAULT '{{}}',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, connector_name)
        );
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_user_oauth_tokens_user_id ON {DB_SCHEMA}.user_oauth_tokens(user_id);")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_user_oauth_tokens_connector ON {DB_SCHEMA}.user_oauth_tokens(connector_name);")
    _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
    try:
        _run_ddl(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.user_oauth_tokens TO {_app_user};")
    except Exception:
        pass
    print("  ok Part U8: user_oauth_tokens table created (S16 FK-mismatch fix)")


def _part_w1_knowledge_graph_2026_06_11():
    """
    Part W1: 2026-06-11 — Unified knowledge graph (code + docs).

    Four tables on PGS01:
      - knowledge_graph_nodes : code symbols (mirrored from code_graph) + doc entities
      - knowledge_graph_edges : first-class directed edges (multi-hop CTE + per-edge RBAC)
      - knowledge_graph_build_status : per-graph_id build state
      - knowledge_graph_domains : LLM-clustered business domains

    RBAC columns mirror document_embeddings so graph queries enforce the same
    access scoping. `code_graph` is left untouched — it is mirrored into these
    tables at index time by workers/index_worker._mirror_code_nodes_to_kg().
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.knowledge_graph_nodes (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            graph_id        VARCHAR(200)  NOT NULL,
            node_id         VARCHAR(1000) NOT NULL,
            node_type       VARCHAR(80)   NOT NULL,
            name            VARCHAR(500)  NOT NULL,
            source_type     VARCHAR(20)   NOT NULL DEFAULT 'code',
            source_ref      TEXT,
            language        VARCHAR(50),
            summary         TEXT,
            metadata        JSONB         NOT NULL DEFAULT '{{}}',
            classification  VARCHAR(50)   NOT NULL DEFAULT 'internal',
            owner_team      VARCHAR(255),
            allowed_roles   JSONB         NOT NULL DEFAULT '[]',
            visibility      VARCHAR(20)   NOT NULL DEFAULT 'PUBLIC',
            min_band_level  SMALLINT      NOT NULL DEFAULT 0,
            product_id      UUID,
            department      VARCHAR(255),
            created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_kg_node UNIQUE (graph_id, node_id)
        );
        CREATE INDEX IF NOT EXISTS idx_kg_nodes_graph ON {DB_SCHEMA}.knowledge_graph_nodes(graph_id);
        CREATE INDEX IF NOT EXISTS idx_kg_nodes_name  ON {DB_SCHEMA}.knowledge_graph_nodes(lower(name));
        CREATE INDEX IF NOT EXISTS idx_kg_nodes_gsrc  ON {DB_SCHEMA}.knowledge_graph_nodes(graph_id, source_type);
        CREATE INDEX IF NOT EXISTS idx_kg_nodes_meta  ON {DB_SCHEMA}.knowledge_graph_nodes USING GIN(metadata);
        CREATE INDEX IF NOT EXISTS idx_kg_nodes_acl   ON {DB_SCHEMA}.knowledge_graph_nodes(classification, min_band_level);
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.knowledge_graph_edges (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            graph_id        VARCHAR(200)  NOT NULL,
            src_node_id     VARCHAR(1000) NOT NULL,
            dst_node_id     VARCHAR(1000) NOT NULL,
            edge_type       VARCHAR(80)   NOT NULL,
            weight          REAL          NOT NULL DEFAULT 1.0,
            metadata        JSONB         NOT NULL DEFAULT '{{}}',
            classification  VARCHAR(50)   NOT NULL DEFAULT 'internal',
            min_band_level  SMALLINT      NOT NULL DEFAULT 0,
            allowed_roles   JSONB         NOT NULL DEFAULT '[]',
            created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_kg_edge UNIQUE (graph_id, src_node_id, dst_node_id, edge_type)
        );
        CREATE INDEX IF NOT EXISTS idx_kg_edges_src  ON {DB_SCHEMA}.knowledge_graph_edges(graph_id, src_node_id);
        CREATE INDEX IF NOT EXISTS idx_kg_edges_dst  ON {DB_SCHEMA}.knowledge_graph_edges(graph_id, dst_node_id);
        CREATE INDEX IF NOT EXISTS idx_kg_edges_type ON {DB_SCHEMA}.knowledge_graph_edges(edge_type);
        CREATE INDEX IF NOT EXISTS idx_kg_edges_hop  ON {DB_SCHEMA}.knowledge_graph_edges(graph_id, src_node_id, edge_type);
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.knowledge_graph_build_status (
            graph_id        VARCHAR(200) PRIMARY KEY,
            status          VARCHAR(40)  NOT NULL DEFAULT 'queued',
            job_id          VARCHAR(100),
            code_nodes      INT          NOT NULL DEFAULT 0,
            doc_nodes       INT          NOT NULL DEFAULT 0,
            cross_edges     INT          NOT NULL DEFAULT 0,
            error           TEXT,
            metadata        JSONB        NOT NULL DEFAULT '{{}}',
            last_built_at   TIMESTAMPTZ,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.knowledge_graph_domains (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            graph_id        VARCHAR(200) NOT NULL,
            domain_name     VARCHAR(255) NOT NULL,
            description     TEXT,
            member_node_ids JSONB        NOT NULL DEFAULT '[]',
            centroid        VARCHAR(1000),
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_kg_domain UNIQUE (graph_id, domain_name)
        );
        CREATE INDEX IF NOT EXISTS idx_kg_domains_graph ON {DB_SCHEMA}.knowledge_graph_domains(graph_id);
    """)
    try:
        _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
        for _t in ("knowledge_graph_nodes", "knowledge_graph_edges",
                   "knowledge_graph_build_status", "knowledge_graph_domains"):
            _run_ddl(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.{_t} TO {_app_user};")
    except Exception:
        pass
    print("  ok Part W1: knowledge_graph tables (nodes/edges/build_status/domains) created")


def _part_x1_external_sync_2026_06_15():
    """
    Part X1: 2026-06-15 — External resource sync status.

    One row per upstream repo (Anthropic/OpenAI skills, security harness, cookbooks,
    plugins) tracked by workers/external_sync_worker.py. Records the imported HEAD SHA
    so re-imports are idempotent (skip when unchanged) and drift is visible. Pure
    bookkeeping — no model coupling.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.external_sync_status (
            repo_id         VARCHAR(120) PRIMARY KEY,
            url             TEXT          NOT NULL,
            importer        VARCHAR(40)   NOT NULL,
            local_path      TEXT,
            head_sha        VARCHAR(64),
            prev_sha        VARCHAR(64),
            pinned_commit   VARCHAR(64),
            importer_result JSONB         NOT NULL DEFAULT '{{}}',
            drift_detected  BOOLEAN       NOT NULL DEFAULT FALSE,
            last_error      TEXT,
            synced_at       TIMESTAMPTZ,
            created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_external_sync_importer ON {DB_SCHEMA}.external_sync_status(importer);
    """)
    try:
        _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
        _run_ddl(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.external_sync_status TO {_app_user};")
    except Exception:
        pass
    print("  ok Part X1: external_sync_status table created")


def _part_z5_coach_2026_06_30():
    """
    2026-06-30 — AiNxt Coach (self-contained feature).

    The ten coach_* tables are defined as ORM models in db/models.py and created
    by create_all() above. This part is belt-and-suspenders: it ensures the tables
    exist with explicit DDL (for environments not bootstrapped via SQLAlchemy),
    adds composite indexes, and GRANTs to the app user. Idempotent.

    Gated by ENABLE_COACH — these tables are inert until the flag is on, and
    nothing else in the platform references them. coach_event is the firehose
    table (one row per interaction); store ONLY a hash + redacted (encrypted)
    prompt, never raw text.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.coach_event (
            event_id        UUID NOT NULL,
            ts              TIMESTAMP NOT NULL DEFAULT NOW(),
            user_id         VARCHAR(255) NOT NULL,
            channel         VARCHAR(32)  NOT NULL,
            workspace       VARCHAR(255),
            project         VARCHAR(255),
            thread_id       VARCHAR(255),
            request_id      VARCHAR(255),
            model           VARCHAR(128),
            prompt_hash     VARCHAR(64),
            prompt_redacted TEXT,
            completion_hash VARCHAR(64),
            tokens_in       INTEGER NOT NULL DEFAULT 0,
            tokens_out      INTEGER NOT NULL DEFAULT 0,
            cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            context_window_pct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            tool_calls      JSONB NOT NULL DEFAULT '[]'::jsonb,
            accepted        BOOLEAN,
            governance_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
            compliance_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
            pii_flags       JSONB NOT NULL DEFAULT '[]'::jsonb,
            secret_flags    JSONB NOT NULL DEFAULT '[]'::jsonb,
            latency_ms      INTEGER NOT NULL DEFAULT 0,
            rule_hits       JSONB NOT NULL DEFAULT '[]'::jsonb,
            department      VARCHAR(255),
            PRIMARY KEY (event_id, ts)
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.coach_rule_hit (
            id          UUID PRIMARY KEY,
            event_id    UUID NOT NULL,
            user_id     VARCHAR(255) NOT NULL,
            rule_id     VARCHAR(64)  NOT NULL,
            category    VARCHAR(64)  NOT NULL,
            severity    VARCHAR(16)  NOT NULL DEFAULT 'low',
            channel     VARCHAR(32)  NOT NULL,
            department  VARCHAR(255),
            detail      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            evidence    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            muted       BOOLEAN NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.coach_score_snapshot (
            id              UUID PRIMARY KEY,
            user_id         VARCHAR(255) NOT NULL,
            snapshot_date   TIMESTAMP NOT NULL DEFAULT NOW(),
            score_overall   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            score_prompt    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            score_session   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            score_review    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            score_tool      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            score_context   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            score_security  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            event_count     INTEGER NOT NULL DEFAULT 0,
            hit_count       INTEGER NOT NULL DEFAULT 0,
            department      VARCHAR(255),
            created_at      TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.coach_rule_pack (
            id          UUID PRIMARY KEY,
            pack_id     VARCHAR(128) NOT NULL,
            version     VARCHAR(32)  NOT NULL DEFAULT '1.0.0',
            name        VARCHAR(255) NOT NULL,
            description TEXT,
            rules       JSONB NOT NULL DEFAULT '[]'::jsonb,
            mandatory   BOOLEAN NOT NULL DEFAULT FALSE,
            published   BOOLEAN NOT NULL DEFAULT TRUE,
            created_by  VARCHAR(255),
            created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.coach_rule_mute (
            id          UUID PRIMARY KEY,
            user_id     VARCHAR(255) NOT NULL,
            rule_id     VARCHAR(64)  NOT NULL,
            muted_until TIMESTAMP,
            reason      TEXT,
            created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_coach_mute_user_rule UNIQUE (user_id, rule_id)
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.coach_rule_disabled (
            id          UUID PRIMARY KEY,
            rule_id     VARCHAR(64)  NOT NULL,
            department  VARCHAR(255),
            reason      TEXT,
            disabled_by VARCHAR(255),
            created_at  TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.coach_admin_audit (
            id          UUID PRIMARY KEY,
            actor_id    VARCHAR(255) NOT NULL,
            actor_email VARCHAR(255),
            action      VARCHAR(64)  NOT NULL,
            target_user VARCHAR(255),
            rule_id     VARCHAR(64),
            details     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            reason      TEXT,
            created_at  TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.coach_manual_note (
            id          UUID PRIMARY KEY,
            user_id     VARCHAR(255) NOT NULL,
            actor_id    VARCHAR(255) NOT NULL,
            actor_email VARCHAR(255),
            kind        VARCHAR(32)  NOT NULL DEFAULT 'nudge',
            subject     VARCHAR(255),
            body        TEXT,
            delivered   BOOLEAN NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.coach_weekly_mail_opt_out (
            id           UUID PRIMARY KEY,
            user_id      VARCHAR(255) NOT NULL UNIQUE,
            opted_out_by VARCHAR(255),
            reason       TEXT,
            created_at   TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    # lookup + composite indexes
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_event_user_ts ON {DB_SCHEMA}.coach_event (user_id, ts)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_event_dept_ts ON {DB_SCHEMA}.coach_event (department, ts)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_event_channel ON {DB_SCHEMA}.coach_event (channel)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_event_request ON {DB_SCHEMA}.coach_event (request_id)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_event_user_request_ts ON {DB_SCHEMA}.coach_event (user_id, request_id, ts DESC)")
    _run_ddl(f"DROP INDEX IF EXISTS {DB_SCHEMA}.idx_coach_event_dedup")
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_coach_event_dedup_lookup
        ON {DB_SCHEMA}.coach_event (user_id, channel, prompt_hash, ts DESC)
        WHERE prompt_hash IS NOT NULL
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_hit_user ON {DB_SCHEMA}.coach_rule_hit (user_id)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_hit_rule ON {DB_SCHEMA}.coach_rule_hit (rule_id)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_hit_event ON {DB_SCHEMA}.coach_rule_hit (event_id)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_hit_created ON {DB_SCHEMA}.coach_rule_hit (created_at)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_score_user ON {DB_SCHEMA}.coach_score_snapshot (user_id)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_mute_user ON {DB_SCHEMA}.coach_rule_mute (user_id)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_disabled_rule ON {DB_SCHEMA}.coach_rule_disabled (rule_id)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_audit_actor ON {DB_SCHEMA}.coach_admin_audit (actor_id)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_coach_note_user ON {DB_SCHEMA}.coach_manual_note (user_id)")
    try:
        _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
        for _t in ("coach_event", "coach_rule_hit", "coach_score_snapshot",
                   "coach_rule_pack", "coach_rule_mute", "coach_rule_disabled",
                   "coach_admin_audit", "coach_manual_note", "coach_weekly_mail_opt_out"):
            _run_ddl(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.{_t} TO {_app_user};")
    except Exception:
        pass
    print("  ✓ Part Z5: coach_* tables (AiNxt Coach)")


def _part_z6_cli_version_registry_2026_07_08():
    """
    2026-07-08 — CLI version registry (fleet visibility for pushing updates).

    Backs the CliVersionRecord ORM model in db/models.py. One row per
    (user_id, install_id) so the same engineer on two boxes shows two rows
    and an update rollout can be targeted at whichever installs are stale.

    Populated by POST /ainxt/v1/api/cli/heartbeat, fired once per REPL boot
    (and every 6h for long-running sessions) from the CLI's telemetry-version
    module. All fields are telemetry-only — no prompts, no filenames, no
    source paths. UPSERT semantics on (user_id, install_id) so successive
    heartbeats bump last_seen_at + session_count without exploding row count.

    Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.cli_version_registry (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         VARCHAR(255) NOT NULL,
            email           VARCHAR(255),
            install_id      VARCHAR(64)  NOT NULL,
            version         VARCHAR(32)  NOT NULL,
            channel         VARCHAR(16)  NOT NULL DEFAULT 'latest',
            binary_name     VARCHAR(64),
            os              VARCHAR(32),
            arch            VARCHAR(16),
            os_release      VARCHAR(128),
            runtime         VARCHAR(32),
            runtime_version VARCHAR(32),
            session_count   INTEGER      NOT NULL DEFAULT 1,
            first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            last_seen_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_cli_version_user_install UNIQUE (user_id, install_id)
        )
    """)
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_cli_version_user "
        f"ON {DB_SCHEMA}.cli_version_registry (user_id)"
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_cli_version_email "
        f"ON {DB_SCHEMA}.cli_version_registry (email) WHERE email IS NOT NULL"
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_cli_version_install "
        f"ON {DB_SCHEMA}.cli_version_registry (install_id)"
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_cli_version_version "
        f"ON {DB_SCHEMA}.cli_version_registry (version)"
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS ix_cli_version_last_seen_version "
        f"ON {DB_SCHEMA}.cli_version_registry (last_seen_at, version)"
    )
    try:
        _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
        _run_ddl(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON "
            f"{DB_SCHEMA}.cli_version_registry TO {_app_user};"
        )
    except Exception:
        pass
    print("  ✓ Part Z6: cli_version_registry (CLI fleet visibility)")

    # ── Part Z7: Owner-scoped governance unique constraints ──────────────────
    # The governance mirror tables (workflows_pg, agents_pg, skills_pg) had
    # unique constraints on (name, org_id) only. On a shared database this meant
    # two users could not independently submit same-named artifacts for approval
    # — the second submit would overwrite the first's governance record, and
    # status lookups leaked across accounts. This drops the old constraint and
    # creates a new one scoped by (name, created_by, org_id) so each user's
    # governance record is independent. Also adds created_by to
    # governance_events for owner-scoped audit history.
    _run_ddl("""
        ALTER TABLE workflows_pg DROP CONSTRAINT IF EXISTS uq_workflows_name_org;
        ALTER TABLE agents_pg    DROP CONSTRAINT IF EXISTS uq_agents_name_org;
        ALTER TABLE skills_pg    DROP CONSTRAINT IF EXISTS uq_skills_name_org;

        -- Guarded: Postgres has no ADD CONSTRAINT IF NOT EXISTS, and
        -- Base.metadata.create_all() already creates these three from the
        -- UniqueConstraint declarations in db/models.py. Unguarded they failed
        -- with `relation "uq_..._name_owner_org" already exists` on every run.
        DO $uq$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_workflows_name_owner_org') THEN
            ALTER TABLE workflows_pg ADD CONSTRAINT uq_workflows_name_owner_org UNIQUE (name, created_by, org_id);
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_agents_name_owner_org') THEN
            ALTER TABLE agents_pg ADD CONSTRAINT uq_agents_name_owner_org UNIQUE (name, created_by, org_id);
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_skills_name_owner_org') THEN
            ALTER TABLE skills_pg ADD CONSTRAINT uq_skills_name_owner_org UNIQUE (name, created_by, org_id);
          END IF;
        END $uq$;

        ALTER TABLE governance_events
            ADD COLUMN IF NOT EXISTS created_by VARCHAR(255);

        CREATE INDEX IF NOT EXISTS idx_gov_events_owner
            ON governance_events (entity_type, name, created_by);
    """)
    print("  ✓ Part Z7: owner-scoped governance constraints + governance_events.created_by")


def _part_aa1_discussions_bot_runs_2026_07_11():
    """
    2026-07-11 — Discussions module (Apache Answer, embedded as a fully
    separate service — see docs/DISCUSSIONS_MODULE_IMPLEMENTATION_PLAN.md).

    This is the ONLY table AiNxt's own Postgres needs for the module: it
    tracks the status of @AiNxt bot replies posted into Apache Answer.
    Everything else (questions, answers, votes, tags, Answer's own users)
    lives in Apache Answer's separate `ainxt_answer` database, not here.

    Gated by ENABLE_DISCUSSIONS — this table is inert until the flag is on,
    and nothing else in the platform references it. Idempotent.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.discussions_bot_runs (
            id               UUID PRIMARY KEY,
            answer_post_id   VARCHAR(128) NOT NULL,
            answer_post_type VARCHAR(16)  NOT NULL,
            mention_author   VARCHAR(255),
            status           VARCHAR(16)  NOT NULL DEFAULT 'pending',
            input_redacted   BOOLEAN      NOT NULL DEFAULT FALSE,
            output_redacted  BOOLEAN      NOT NULL DEFAULT FALSE,
            error_message    TEXT,
            reply_post_id    VARCHAR(128),
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_discussions_bot_runs_status ON {DB_SCHEMA}.discussions_bot_runs (status)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_discussions_bot_runs_answer_post_id ON {DB_SCHEMA}.discussions_bot_runs (answer_post_id)")
    try:
        _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
        _run_ddl(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.discussions_bot_runs TO {_app_user};")
    except Exception:
        pass
    print("  ✓ Part AA1: discussions_bot_runs (Discussions module @AiNxt bot bookkeeping)")


def _part_aa2_discussions_mirror_2026_07_11():
    """
    2026-07-11 — Discussions module, third revision: native ai-ui frontend +
    Apache Answer as a headless internal engine (services/discussions_engine/),
    not a separate browser-facing app. See docs/DISCUSSIONS_MODULE_IMPLEMENTATION_PLAN.md
    "Revision history".

    Every write to the headless engine is mirrored into these AiNxt-native
    tables in the SAME gateway request that performs it (routers/discussions_router.py).
    discussions_events is the actual feedback-spine substrate: an append-only
    log of every meaningful action, same shape as the existing skill_loop_worker.py
    signal-capture pattern, designed to be consumed by a future self-improvement
    worker with zero cross-database queries.

    Gated by ENABLE_DISCUSSIONS — inert until the flag is on. Idempotent.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.discussions_questions (
            id                 UUID PRIMARY KEY,
            external_id        VARCHAR(128) NOT NULL,
            author_user_id     VARCHAR(255) NOT NULL,
            title              VARCHAR(500) NOT NULL,
            content            TEXT NOT NULL,
            tags               JSONB NOT NULL DEFAULT '[]'::jsonb,
            vote_count         INTEGER NOT NULL DEFAULT 0,
            answer_count       INTEGER NOT NULL DEFAULT 0,
            accepted_answer_id UUID,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.discussions_answers (
            id             UUID PRIMARY KEY,
            external_id    VARCHAR(128) NOT NULL,
            question_id    UUID NOT NULL REFERENCES {DB_SCHEMA}.discussions_questions(id) ON DELETE CASCADE,
            author_user_id VARCHAR(255) NOT NULL,
            content        TEXT NOT NULL,
            vote_count     INTEGER NOT NULL DEFAULT 0,
            is_accepted    BOOLEAN NOT NULL DEFAULT FALSE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.discussions_votes (
            id          UUID PRIMARY KEY,
            target_type VARCHAR(16) NOT NULL,
            target_id   UUID NOT NULL,
            user_id     VARCHAR(255) NOT NULL,
            direction   SMALLINT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_discussions_vote UNIQUE (target_type, target_id, user_id)
        )
    """)
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.discussions_events (
            id            UUID PRIMARY KEY,
            event_type    VARCHAR(40) NOT NULL,
            actor_user_id VARCHAR(255),
            target_type   VARCHAR(16),
            target_id     UUID,
            payload       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    for _idx in [
        f"CREATE INDEX IF NOT EXISTS idx_discussions_questions_author ON {DB_SCHEMA}.discussions_questions (author_user_id)",
        f"CREATE INDEX IF NOT EXISTS idx_discussions_answers_question ON {DB_SCHEMA}.discussions_answers (question_id)",
        f"CREATE INDEX IF NOT EXISTS idx_discussions_answers_author ON {DB_SCHEMA}.discussions_answers (author_user_id)",
        f"CREATE INDEX IF NOT EXISTS idx_discussions_votes_target ON {DB_SCHEMA}.discussions_votes (target_type, target_id)",
        f"CREATE INDEX IF NOT EXISTS idx_discussions_events_type ON {DB_SCHEMA}.discussions_events (event_type)",
        f"CREATE INDEX IF NOT EXISTS idx_discussions_events_actor ON {DB_SCHEMA}.discussions_events (actor_user_id)",
        f"CREATE INDEX IF NOT EXISTS idx_discussions_events_created ON {DB_SCHEMA}.discussions_events (created_at)",
    ]:
        _run_ddl(_idx)
    try:
        _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
        for _t in ("discussions_questions", "discussions_answers", "discussions_votes", "discussions_events"):
            _run_ddl(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.{_t} TO {_app_user};")
    except Exception:
        pass
    print("  ✓ Part AA2: discussions_questions/answers/votes/events (native mirror + feedback-spine log)")


def _part_aa3_discussions_comments_2026_07_11():
    """
    2026-07-11 — Discussions module: comments mirror. Same pattern as
    discussions_answers — every comment posted via services/discussions_engine
    is mirrored here in the same gateway request, and @AiNxt mentions inside
    comments trigger the bot the same way as questions/answers.

    Gated by ENABLE_DISCUSSIONS — inert until the flag is on. Idempotent.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.discussions_comments (
            id             UUID PRIMARY KEY,
            external_id    VARCHAR(128) NOT NULL,
            target_type    VARCHAR(16)  NOT NULL,   -- question | answer
            target_id      UUID         NOT NULL,
            author_user_id VARCHAR(255) NOT NULL,
            content        TEXT         NOT NULL,
            created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_discussions_comments_target ON {DB_SCHEMA}.discussions_comments (target_type, target_id)")
    _run_ddl(f"CREATE INDEX IF NOT EXISTS idx_discussions_comments_author ON {DB_SCHEMA}.discussions_comments (author_user_id)")
    try:
        _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
        _run_ddl(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.discussions_comments TO {_app_user};")
    except Exception:
        pass
    print("  ✓ Part AA3: discussions_comments mirror table")


def _part_aa4_discussions_timestamp_fix_2026_07_12():
    """
    2026-07-12 — Fix a real timestamp-correctness bug scoped to Discussions'
    own 6 tables (does not touch db/models.py::_now(), which every other
    table in this schema also uses, or any non-Discussions table).

    Every write goes through the shared `_now()` (naive `datetime.utcnow()`
    — the platform-wide convention, matching plain TIMESTAMP columns like
    `chats.created_at`). But these 6 tables were created with TIMESTAMPTZ
    columns instead. When this deployment's Postgres `TimeZone` setting is
    not UTC (confirmed here: `SHOW timezone` → Asia/Kolkata), a naive
    UTC-clock value landing in a TIMESTAMPTZ column gets silently
    re-labeled by Postgres's input parser as being IN the session timezone
    (IST) rather than UTC — every Discussions timestamp ends up recorded
    5.5 hours earlier than the real moment it happened.

    Confirmed empirically before writing this migration: inserted a probe
    row at real UTC 19:54:32 (real IST 01:24:32 the next calendar day) —
    it came back as `2026-07-11 19:54:32+05:30`, i.e. the correct naive
    UTC digits, mislabeled with an IST offset instead of being converted.

    Fix: convert the columns to plain TIMESTAMP (the platform convention),
    correcting the already-stored rows in the same statement —
    `AT TIME ZONE 'Asia/Kolkata'` reconverts each TIMESTAMPTZ's absolute
    instant back into IST wall-clock digits, which by construction recovers
    the exact naive UTC digits `_now()` originally wrote (see the worked
    example above). This is a genuine data correction, not just a type
    change — do not simplify this to a bare `::timestamp` cast, which would
    truncate the (wrong) offset instead of fixing the underlying value.

    Idempotent: once a column is plain TIMESTAMP, re-running this
    (session TZ still Asia/Kolkata) round-trips to the same values —
    interpret-as-Asia/Kolkata then store-as-Asia/Kolkata-wall-clock is a
    no-op the second time. Safe to run repeatedly.
    """
    for _table, _cols in [
        ("discussions_bot_runs", ["created_at", "updated_at"]),
        ("discussions_questions", ["created_at", "updated_at"]),
        ("discussions_answers", ["created_at", "updated_at"]),
        ("discussions_votes", ["created_at"]),
        ("discussions_events", ["created_at"]),
        ("discussions_comments", ["created_at"]),
    ]:
        for _col in _cols:
            _run_ddl(f"""
                ALTER TABLE {DB_SCHEMA}.{_table}
                ALTER COLUMN {_col} TYPE TIMESTAMP
                USING {_col} AT TIME ZONE 'Asia/Kolkata'
            """)
    print("  ✓ Part AA4: fixed Discussions TIMESTAMPTZ timezone-mislabeling bug (6 tables)")


def _part_aa5_discussions_comment_count_2026_07_12():
    """
    2026-07-12 — Denormalized comment_count on discussions_questions/answers,
    same pattern as the existing answer_count/vote_count columns. Needed so
    the "Add / view comments" toggle can show a real count without an N+1
    fetch per row (comments aren't loaded until the toggle is opened).
    Backfilled from the real discussions_comments rows for data already
    written before this column existed. Idempotent.
    """
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.discussions_questions ADD COLUMN IF NOT EXISTS comment_count INTEGER NOT NULL DEFAULT 0")
    _run_ddl(f"ALTER TABLE {DB_SCHEMA}.discussions_answers ADD COLUMN IF NOT EXISTS comment_count INTEGER NOT NULL DEFAULT 0")
    _run_ddl(f"""
        UPDATE {DB_SCHEMA}.discussions_questions q SET comment_count = (
            SELECT COUNT(*) FROM {DB_SCHEMA}.discussions_comments c
            WHERE c.target_type = 'question' AND c.target_id = q.id
        )
    """)
    _run_ddl(f"""
        UPDATE {DB_SCHEMA}.discussions_answers a SET comment_count = (
            SELECT COUNT(*) FROM {DB_SCHEMA}.discussions_comments c
            WHERE c.target_type = 'answer' AND c.target_id = a.id
        )
    """)
    print("  ✓ Part AA5: discussions_questions/answers.comment_count (backfilled)")


def _part_oss1_department_hod_mapping_2026_07_29():
    """
    2026-07-29 — OSS GAP-18: Create department_hod_mapping table if missing.

    In the originating deployment this table was DBA-owned (skip_autogenerate=True in models.py)
    and never created by migrate.py. For OSS users running python db/migrate.py
    the table was never created, causing budget_router, governance_router, and
    hod_statement_service to fail with "relation does not exist".

    This migration creates the table with CREATE TABLE IF NOT EXISTS — safe to
    run where the table already exists (no-op) and on a fresh install (creates it).

    Also creates hod_allocation_caps and hod_allocation_ledger which are
    referenced by budget_router and were similarly DBA-owned there.
    """
    from sqlalchemy import text as _text
    from db.database import engine as _engine, DB_SCHEMA as _schema

    ddl_statements = [
        # ── department_hod_mapping ────────────────────────────────────────────
        f"""
        CREATE TABLE IF NOT EXISTS {_schema}.department_hod_mapping (
            department_name           VARCHAR(255) NOT NULL,
            corrected_department_name VARCHAR(255),
            hod_name                  VARCHAR(255),
            hod_email                 VARCHAR(255) NOT NULL,
            PRIMARY KEY (department_name, hod_email)
        )
        """,
        f"""
        CREATE INDEX IF NOT EXISTS idx_dept_hod_mapping_email
            ON {_schema}.department_hod_mapping (lower(hod_email))
        """,
        # ── hod_allocation_caps ───────────────────────────────────────────────
        f"""
        CREATE TABLE IF NOT EXISTS {_schema}.hod_allocation_caps (
            hod_email   VARCHAR(255) NOT NULL PRIMARY KEY,
            cap_usd     NUMERIC(12,4) NOT NULL DEFAULT 0,
            notes       TEXT,
            updated_at  TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
        """,
        # ── hod_allocation_ledger ─────────────────────────────────────────────
        f"""
        CREATE TABLE IF NOT EXISTS {_schema}.hod_allocation_ledger (
            id              BIGSERIAL PRIMARY KEY,
            hod_email       VARCHAR(255) NOT NULL,
            action          VARCHAR(50)  NOT NULL,
            amount_usd      NUMERIC(12,4) NOT NULL DEFAULT 0,
            reference_id    VARCHAR(255),
            note            TEXT,
            created_at      TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
        """,
        f"""
        CREATE INDEX IF NOT EXISTS idx_hod_ledger_email
            ON {_schema}.hod_allocation_ledger (lower(hod_email))
        """,
    ]

    try:
        with _engine.connect() as conn:
            for stmt in ddl_statements:
                conn.execute(_text(stmt))
            conn.commit()
        print("  ok department_hod_mapping + hod_allocation_caps + hod_allocation_ledger created (or already exist)")
    except Exception as e:
        print(f"  ! department_hod_mapping DDL warning: {e}")


def _part_oss2_temp_password_flag_2026_07_29():
    """
    2026-07-29 — OSS GAP-6: Add is_temp_password flag to users table.

    When an OSS user triggers "Forgot password?", the backend generates a
    temporary password, sets is_temp_password=True, and delivers it via
    email (SMTP) or server console log (no-SMTP fallback).

    After the user logs in with the temp password and changes it via
    Profile → Change Password, is_temp_password is cleared to False.

    The Profile UI reads this flag to show an amber "You are using a
    temporary password — please change it now" banner.

    Idempotent: ADD COLUMN IF NOT EXISTS — safe on an existing deployment
    (no-op if the column already exists) and on a fresh install.
    Directory-backed deployments: LDAP users never have hashed_password set,
    so this column is always False for them and the banner never shows.
    """
    _run_ddl(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "is_temp_password BOOLEAN NOT NULL DEFAULT FALSE",
        "GAP-6: users.is_temp_password flag",
    )


def _part_oss2_user_employee_id_2026_08_02():
    """
    2026-08-02 — Add employee_id column to users table.
    Stores the numeric employeeID attribute fetched from Active Directory.
    Idempotent — safe to run multiple times.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.users
        ADD COLUMN IF NOT EXISTS employee_id VARCHAR(100) NULL;
    """)
    _run_ddl(f"""
        COMMENT ON COLUMN {DB_SCHEMA}.users.employee_id
        IS 'Numeric employeeID from Active Directory (populated by LDAP login and nightly ad_sync)';
    """)
    print("  \u2713 Part OSS2: users.employee_id column (AD employeeID)")


def _part_aa7_source_channel_remap_2026_08_03():
    """
    2026-08-03 — Remap source_channel values to prefixed WEB-* / DESKTOP-* scheme.

    Previously source_channel used flat values (CHAT, BUDDY, DESKTOP, IDE).
    The new scheme distinguishes web-browser vs Electron-desktop traffic:

      CHAT    \u2192 WEB-CHAT       (browser using Chat)
      BUDDY   \u2192 WEB-BUDDY      (browser using Buddy/Cowork)
      DESKTOP \u2192 DESKTOP-CHAT   (desktop app using Chat, from /ask)
              \u2192 DESKTOP-IDE    (desktop app using IDE tab, from /v1/chat/completions)
              \u2192 DESKTOP-CHAT   (catch-all for any remaining DESKTOP rows)

    Standalone clients keep their existing values unchanged:
      CLI, IDE, BROWSER-AGENT, API, AGENT-STUDIO, AGENTS, SDLC

    All UPDATEs are idempotent — safe to re-run.
    """
    # Step 1: backfill NULLs from endpoint
    _run_ddl(
        """
        UPDATE model_usages SET source_channel =
          CASE
            WHEN endpoint = '/v1/messages'                                       THEN 'CLI'
            WHEN endpoint = '/v1/chat/completions'                               THEN 'IDE'
            WHEN endpoint IN ('/ask', '/ask/cached', '/chat/video-generate')     THEN 'WEB-CHAT'
            WHEN endpoint = '/sdlc/pipeline'                                     THEN 'SDLC'
            WHEN endpoint LIKE 'abstudio.agent.%%'                               THEN 'AGENT-STUDIO'
            WHEN endpoint LIKE '/agents/%%/run'                                  THEN 'AGENTS'
            ELSE NULL
          END
        WHERE source_channel IS NULL AND endpoint IS NOT NULL
        """,
        "model_usages.source_channel NULL backfill (AA7)",
    )
    _run_ddl(
        "UPDATE model_usages SET source_channel = 'WEB-CHAT' WHERE source_channel = 'CHAT'",
        "model_usages.source_channel CHAT\u2192WEB-CHAT",
    )
    _run_ddl(
        "UPDATE model_usages SET source_channel = 'WEB-BUDDY' WHERE source_channel = 'BUDDY'",
        "model_usages.source_channel BUDDY\u2192WEB-BUDDY",
    )
    _run_ddl(
        """
        UPDATE model_usages SET source_channel = 'DESKTOP-CHAT'
        WHERE source_channel = 'DESKTOP'
          AND endpoint IN ('/ask', '/ask/cached', '/chat/video-generate')
        """,
        "model_usages.source_channel DESKTOP\u2192DESKTOP-CHAT (/ask)",
    )
    _run_ddl(
        """
        UPDATE model_usages SET source_channel = 'DESKTOP-IDE'
        WHERE source_channel = 'DESKTOP'
          AND endpoint = '/v1/chat/completions'
        """,
        "model_usages.source_channel DESKTOP\u2192DESKTOP-IDE (/v1/chat/completions)",
    )
    _run_ddl(
        "UPDATE model_usages SET source_channel = 'DESKTOP-CHAT' WHERE source_channel = 'DESKTOP'",
        "model_usages.source_channel DESKTOP\u2192DESKTOP-CHAT (catch-all)",
    )
    print("  \u2713 Part AA7: source_channel remapped to WEB-*/DESKTOP-* scheme")
    # Coach eval engine columns (chained from AA7)
    _part_aa7_coach_eval_columns_2026_07_29()


def _part_aa6_endpoint_model_ids_2026_07_24():
    """
    2026-07-24 — Endpoint multi-model allowlist.

    Replaces the single model_id VARCHAR(255) column with a model_ids JSONB array
    that holds the list of local models allowed for this endpoint. Callers may
    use any model in the list; no silent override is applied.

    Changes:
      - ADD model_ids JSONB nullable (list of allowed model IDs)
      - Migrate existing model_id value → model_ids[0] where not null
      - DROP model_id column (no longer used by the application)

    Idempotent: IF NOT EXISTS / IF EXISTS / WHERE guards throughout.
    """
    # Add the new model_ids JSONB column
    _run_ddl(
        "ALTER TABLE managed_endpoints ADD COLUMN IF NOT EXISTS model_ids JSONB",
        "Part AA6: managed_endpoints ADD model_ids JSONB",
    )
    # Migrate existing single model_id value → model_ids array (only if model_id column exists)
    _run_ddl(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'managed_endpoints' AND column_name = 'model_id'
            ) THEN
                UPDATE managed_endpoints
                   SET model_ids = jsonb_build_array(model_id)
                 WHERE model_id IS NOT NULL AND model_ids IS NULL;
            END IF;
        END$$
        """,
        "Part AA6: managed_endpoints backfill model_ids from model_id",
    )
    # Drop the old model_id column — no longer used
    _run_ddl(
        "ALTER TABLE managed_endpoints DROP COLUMN IF EXISTS model_id",
        "Part AA6: managed_endpoints DROP model_id",
    )
    print("  ok Part AA6: managed_endpoints.model_ids JSONB added, model_id dropped")


def _part_aa7_coach_eval_columns_2026_07_29():
    """
    2026-07-29 — Add LLM-as-judge eval results to coach_event.

    The EvalEngine (core/evals.py) now runs eval_coach_prompt() on every
    ingested CoachEvent as a second, independent validation layer alongside
    the deterministic rule evaluator (agents/coach_evaluator.py).

    Results are written back asynchronously (daemon thread) after the event
    is persisted, so these columns start as NULL and are filled in within
    ~15 s of ingestion.

    New columns on coach_event:
      eval_score    FLOAT        — 0.0–1.0 judge score (NULL = not yet evaluated)
      eval_verdict  VARCHAR(16)  — "ACCEPT" | "REJECT" (NULL = not yet evaluated)
      eval_issues   JSONB        — list of specific issue strings from the judge

    Idempotent: ADD COLUMN IF NOT EXISTS throughout.
    """
    _run_ddl(
        "ALTER TABLE coach_event ADD COLUMN IF NOT EXISTS eval_score FLOAT",
        "Part AA7: coach_event ADD eval_score FLOAT",
    )
    _run_ddl(
        "ALTER TABLE coach_event ADD COLUMN IF NOT EXISTS eval_verdict VARCHAR(16)",
        "Part AA7: coach_event ADD eval_verdict VARCHAR(16)",
    )
    _run_ddl(
        "ALTER TABLE coach_event ADD COLUMN IF NOT EXISTS eval_issues JSONB",
        "Part AA7: coach_event ADD eval_issues JSONB",
    )
    print("  ok Part AA7: coach_event eval_score / eval_verdict / eval_issues added")


def _part_ab1_kb_doc_deletions_2026_08_06():
    """
    2026-08-06 — KB Deletion History.

    Adds knowledge_doc_deletions: a snapshot of a knowledge_docs row written
    at the moment it is hard-deleted, ONLY when the doc's status was ACTIVE
    (i.e. it had gone through approval + indexing and was RAG-searchable at
    some point). Docs deleted while still PENDING_APPROVAL or REJECTED never
    went live, so no history row is written for them (see
    store/docs_store.py::delete_doc()).

    Lets admins / HODs / uploaders audit what was removed from the KB even
    after the knowledge_docs row and its document_embeddings chunks are gone.
    See GET /kb/deleted-history (routers/docs_router.py) for the ACL rules
    applied when reading this table.

    Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.knowledge_doc_deletions (
            id                UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
            doc_id            UUID          NOT NULL,
            name              VARCHAR(512)  NOT NULL,
            filename          VARCHAR(512)  NOT NULL,
            namespace         VARCHAR(128)  NOT NULL,
            file_size         INTEGER       DEFAULT 0,
            chunk_count       INTEGER       DEFAULT 0,
            visibility        VARCHAR(20)   NOT NULL DEFAULT 'PUBLIC',
            department_ids    JSONB         NOT NULL DEFAULT '[]',
            product_id        UUID,
            domain            VARCHAR(100),
            spec_version      VARCHAR(50),
            source_type       VARCHAR(32),
            original_ext      VARCHAR(16),
            status            VARCHAR(30)   NOT NULL DEFAULT 'ACTIVE',
            uploaded_by       VARCHAR(255),
            uploaded_by_dept  VARCHAR(255),
            approved_by       VARCHAR(255),
            approved_at       TIMESTAMP,
            doc_created_at    TIMESTAMPTZ,
            deleted_by        VARCHAR(255)  NOT NULL,
            deleted_by_dept   VARCHAR(255),
            deleted_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        )
    """, "Part AB1: knowledge_doc_deletions table created")
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_kdocs_del_doc_id ON {DB_SCHEMA}.knowledge_doc_deletions (doc_id)",
        "Part AB1: idx_kdocs_del_doc_id",
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_kdocs_del_namespace ON {DB_SCHEMA}.knowledge_doc_deletions (namespace)",
        "Part AB1: idx_kdocs_del_namespace",
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_kdocs_del_visibility ON {DB_SCHEMA}.knowledge_doc_deletions (visibility)",
        "Part AB1: idx_kdocs_del_visibility",
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_kdocs_del_product_id ON {DB_SCHEMA}.knowledge_doc_deletions (product_id)",
        "Part AB1: idx_kdocs_del_product_id",
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_kdocs_del_uploaded_by ON {DB_SCHEMA}.knowledge_doc_deletions (uploaded_by)",
        "Part AB1: idx_kdocs_del_uploaded_by",
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_kdocs_del_uploaded_by_dept ON {DB_SCHEMA}.knowledge_doc_deletions (uploaded_by_dept)",
        "Part AB1: idx_kdocs_del_uploaded_by_dept",
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_kdocs_del_deleted_by ON {DB_SCHEMA}.knowledge_doc_deletions (deleted_by)",
        "Part AB1: idx_kdocs_del_deleted_by",
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_kdocs_del_deleted_by_dept ON {DB_SCHEMA}.knowledge_doc_deletions (deleted_by_dept)",
        "Part AB1: idx_kdocs_del_deleted_by_dept",
    )
    _run_ddl(
        f"CREATE INDEX IF NOT EXISTS idx_kdocs_del_deleted_at ON {DB_SCHEMA}.knowledge_doc_deletions (deleted_at DESC)",
        "Part AB1: idx_kdocs_del_deleted_at",
    )
    try:
        _app_user = os.getenv("POSTGRES_USER", "ainxt_app")
        _run_ddl(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB_SCHEMA}.knowledge_doc_deletions TO {_app_user};",
            "Part AB1: grant knowledge_doc_deletions to app user",
        )
    except Exception:
        pass
    print("  ok Part AB1: knowledge_doc_deletions ready (KB Deletion History)")


def _part_aa8_codewiki_docs_2026_07_23():
    """
    2026-07-23 — Add codewiki_doc_jobs table for standalone CodeWiki docs generation.
    Each codebase_name is a unique, user-friendly identifier; the same (repo_url, branch)
    pair cannot be documented twice. Regeneration overwrites the existing row.
    """
    _run_ddl(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.codewiki_doc_jobs (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            codebase_name  TEXT NOT NULL UNIQUE,
            repo_url       TEXT NOT NULL,
            branch         TEXT NOT NULL DEFAULT 'main',
            status         TEXT NOT NULL DEFAULT 'pending',
            error_message  TEXT,
            output_dir     TEXT,
            pages          JSONB DEFAULT '[]'::jsonb,
            created_at     TIMESTAMPTZ DEFAULT NOW(),
            updated_at     TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (repo_url, branch)
        )
    """)
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS idx_codewiki_doc_jobs_codebase_name
            ON {DB_SCHEMA}.codewiki_doc_jobs (codebase_name)
    """)
    print("  ✓ Part AA8: codewiki_doc_jobs table created")


def _part_aa9_codewiki_docs_logs_2026_08_07():
    """
    2026-08-07 — Add `logs` column to codewiki_doc_jobs.

    The worker now shells out to the real `codewiki generate --github-pages
    --verbose --output <dir>` CLI (the same command an operator would run by
    hand) instead of calling the generator library directly, so behaviour is
    guaranteed identical to a manual terminal run. `logs` captures that
    subprocess's combined stdout/stderr so the UI can show the same terminal
    output live while a wiki is generating.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.codewiki_doc_jobs
            ADD COLUMN IF NOT EXISTS logs TEXT NOT NULL DEFAULT ''
    """)
    print("  ✓ Part AA9: codewiki_doc_jobs.logs column added")


def _part_aa10_codewiki_docs_last_commit_sha_2026_08_10():
    """
    2026-08-10 — Add `last_commit_sha` column to codewiki_doc_jobs.

    This column was added directly against the live database when the
    /regenerate dry-run flow (routers/codewiki_router.py) and the worker's
    own commit-tracking (workers/codewiki_worker.py's
    `commit_sha = GitRepo(...).head.commit.hexsha` /
    `_update_job(..., last_commit_sha=commit_sha)`) were first built, but
    this migration was never added at the time -- a fresh database that
    only ever ran `db/migrate.py` would be missing it, and the worker's
    first successful job would fail with an UndefinedColumn error the
    moment it tried to record the completed commit. Backfilling the
    migration here so `python db/migrate.py` alone is sufficient to
    provision the full, real schema this feature actually needs.

    Records the commit that was actually documented, so a later
    /regenerate can diff HEAD against it to compute an incremental
    (rather than full) set of modules to touch.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.codewiki_doc_jobs
            ADD COLUMN IF NOT EXISTS last_commit_sha VARCHAR(64)
    """)
    print("  ✓ Part AA10: codewiki_doc_jobs.last_commit_sha column added")


def _part_aa11_eval_results_judge_model_2026_08_14():
    """
    2026-08-14 — Add `judge_model` column to eval_results.

    Records which LLM acted as the judge when scoring a response, so the
    Eval Observatory can answer "Is GLM-5.2 a stricter judge than DeepSeek?"
    and detect score shifts caused by judge model changes.

    Idempotent: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.eval_results
            ADD COLUMN IF NOT EXISTS judge_model VARCHAR(255)
    """, "eval_results.judge_model column (AA11)")
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS ix_eval_results_judge_model
            ON {DB_SCHEMA}.eval_results (judge_model)
    """, "ix_eval_results_judge_model index (AA11)")
    print("  ✓ Part AA11: eval_results.judge_model column + index added")


def _part_aa12_eval_results_model_2026_08_14():
    """
    2026-08-14 — Add `model` column to eval_results.

    Records which model generated the answer being judged (groundedness rows
    only). Lets the dashboard answer "which model hallucinates more?" by
    grouping groundedness scores by source model.

    Idempotent: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.eval_results
            ADD COLUMN IF NOT EXISTS model VARCHAR(255)
    """, "eval_results.model column (AA12)")
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS ix_eval_results_model
            ON {DB_SCHEMA}.eval_results (model)
    """, "ix_eval_results_model index (AA12)")
    print("  ✓ Part AA12: eval_results.model column + index added")


def _part_aa13_eval_results_platform_2026_08_15():
    """
    2026-08-15 — Add `platform` column to eval_results.

    Records the source platform (chat, knowledge_base, agent_studio, etc.)
    so the Eval Observatory can filter/aggregate by surface. Without this
    column, eval rows written by _persist() silently fail on older schemas
    (the INSERT may drop the entire row depending on ORM/driver behaviour).

    Idempotent: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.eval_results
            ADD COLUMN IF NOT EXISTS platform VARCHAR(64)
    """, "eval_results.platform column (AA13)")
    _run_ddl(f"""
        CREATE INDEX IF NOT EXISTS ix_eval_results_platform
            ON {DB_SCHEMA}.eval_results (platform)
    """, "ix_eval_results_platform index (AA13)")
    print("  ✓ Part AA13: eval_results.platform column + index added")


def _part_aa14_api_key_expires_at_2026_08_17():
    """
    2026-08-17 — Add expires_at + last_expiry_notified_at to user_api_keys.

    API keys expire after 180 days (6 months). The auth layer rejects expired
    keys inline and sends one email reminder when the key enters the 15-day
    warning window. This migration adds the columns and backfills existing keys.
    """
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.user_api_keys
            ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP
    """)
    _run_ddl(f"""
        ALTER TABLE {DB_SCHEMA}.user_api_keys
            ADD COLUMN IF NOT EXISTS last_expiry_notified_at TIMESTAMP
    """)
    # Backfill: set expiration for existing active keys that don't have it
    _key_lifetime_days = int(os.getenv("API_KEY_LIFETIME_DAYS", "180"))
    _run_ddl(f"""
        UPDATE {DB_SCHEMA}.user_api_keys
        SET expires_at = created_at + INTERVAL '{_key_lifetime_days} days'
        WHERE expires_at IS NULL AND is_active = TRUE
    """)
    print("  ✓ Part AA14: user_api_keys.expires_at + last_expiry_notified_at added, existing keys backfilled")



# ── Post-migration verification ─────────────────────────────────────────────
# Objects that the application queries unconditionally on a default install. If
# any is missing the platform will 500 at runtime, so a migration that leaves one
# absent must not be reported as success.
_REQUIRED_SCHEMA = [
    ("users", None),
    ("agents_pg", None),
    ("chats", None),
    ("model_rate_table", None),
    ("codewiki_doc_jobs", None),
    ("dept_model_permissions", "web_search_allowed"),
    ("user_model_permissions", "web_search_allowed"),
    ("graph_audit_log", None),
    ("users", "hod_email"),
]


def _verify_schema() -> list:
    """Return a list of human-readable descriptions of missing schema objects."""
    from sqlalchemy import text as _text
    missing = []
    try:
        with engine.connect() as conn:
            for table, column in _REQUIRED_SCHEMA:
                if column is None:
                    found = conn.execute(_text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = :s AND table_name = :t"
                    ), {"s": DB_SCHEMA, "t": table}).scalar()
                    if not found:
                        missing.append(f"table {DB_SCHEMA}.{table}")
                else:
                    found = conn.execute(_text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = :s AND table_name = :t "
                        "AND column_name = :c"
                    ), {"s": DB_SCHEMA, "t": table, "c": column}).scalar()
                    if not found:
                        missing.append(f"column {DB_SCHEMA}.{table}.{column}")
    except Exception as exc:
        missing.append(f"verification query failed: {exc}")
    return missing


def _report_and_exit() -> None:
    """Print a migration summary and exit non-zero if anything is wrong.

    Before this existed, db/migrate.py printed per-part errors and still exited
    0. A database missing tables and columns therefore looked like a clean run to
    a newcomer following the README and to any CI checking the exit code.

    Set MIGRATE_ALLOW_PARTIAL=true to downgrade this to a warning (useful when
    intentionally migrating a partially-provisioned legacy database).
    """
    missing = _verify_schema()
    if not _MIGRATION_FAILURES and not missing:
        print("\n✓ Migration complete — no failures, required schema present.")
        return

    print("\n" + "=" * 62)
    print("  MIGRATION COMPLETED WITH PROBLEMS")
    print("=" * 62)
    if _MIGRATION_FAILURES:
        print(f"  {len(_MIGRATION_FAILURES)} statement failure(s):")
        for f in _MIGRATION_FAILURES:
            print(f"    - {f}")
    if missing:
        print(f"  {len(missing)} required schema object(s) missing:")
        for m in missing:
            print(f"    - {m}")
        print("  The application will return HTTP 500 on endpoints that use these.")
    print("=" * 62)

    if os.getenv("MIGRATE_ALLOW_PARTIAL", "").strip().lower() in ("1", "true", "yes"):
        print("  MIGRATE_ALLOW_PARTIAL is set — continuing despite the above.\n")
        return
    print("  Exiting non-zero. Set MIGRATE_ALLOW_PARTIAL=true to override.\n")
    sys.exit(1)


if __name__ == "__main__":
    run_migrations()
