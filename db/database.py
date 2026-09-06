# SPDX-License-Identifier: MIT
# ============================================================
# DATABASE — SQLAlchemy engine, session, and dependency
# ============================================================

import os
from contextlib import contextmanager
from sqlalchemy import create_engine, MetaData
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker, DeclarativeBase

def _cfg(key: str, default: str = "") -> str:
    """Read a configuration value from the environment.
    Generic accessor used for all connection parameters including credentials.
    """
    return os.environ.get(key, default)

# Schema name for all application tables on both PGS01 and PGS02.
# Sourced from core.config so APP_OWNER drives the default consistently.
from core.config import POSTGRES_SCHEMA as DB_SCHEMA, POSTGRES_DB as _POSTGRES_DB_DEFAULT

_CONNECT_ARGS = {
    "connect_timeout": 10,
    # Enforce statement timeout; set search_path so all unqualified
    # table names resolve to the ainxt schema.  The public schema
    # is kept second so PostgreSQL extension functions (gen_random_uuid,
    # similarity, etc.) are still accessible without schema-qualification.
    "options": f"-c statement_timeout=60000 -c search_path={DB_SCHEMA},public",
}

# --------------------------------------------------------
# Main Postgres (PGS01) — ORM tables, chat, SDLC, agents
# --------------------------------------------------------

_DB_USER = os.getenv("POSTGRES_USER", "postgres")
# No hardcoded localhost default — reuse core.config.POSTGRES_HOST (itself
# no-default) so this module and core.config never disagree about what
# "unset" resolves to. An empty host fails create_engine's connection
# attempt at first use, same as any other missing required setting.
from core.config import POSTGRES_HOST as _POSTGRES_HOST_DEFAULT
_DB_HOST = os.getenv("POSTGRES_HOST", _POSTGRES_HOST_DEFAULT)
_DB_PORT = os.getenv("POSTGRES_PORT", "5432")
_DB_NAME = os.getenv("POSTGRES_DB", _POSTGRES_DB_DEFAULT)

engine = create_engine(
    URL.create(
        drivername="postgresql+psycopg2",
        username=_DB_USER,
        password=_cfg("POSTGRES_PASSWORD", "postgres"),
        host=_DB_HOST,
        port=int(_DB_PORT),
        database=_DB_NAME,
        query={"options": f"-csearch_path={DB_SCHEMA},public"},
    ),
    pool_pre_ping=True,
    # This primary pool is the ONE pool for the whole in-process app: the
    # platform's own routers AND ABStudio (workflow/checkpoint/agent-chat,
    # which formerly ran ~80 dedicated connections of their own). Sized to
    # absorb that combined load; override via POSTGRES_POOL_* if Postgres
    # max_connections / PgBouncer limits require different tuning.
    pool_size=int(os.getenv("POSTGRES_POOL_SIZE", "50")),
    max_overflow=int(os.getenv("POSTGRES_POOL_MAX_OVERFLOW", "25")),  # burst → 75
    pool_timeout=30,
    pool_recycle=1800,
    connect_args=_CONNECT_ARGS,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# --------------------------------------------------------
# pgVector — document_embeddings, HNSW index
# Set PGVECTOR_HOST to a dedicated pgVector server in prod; falls back to Postgres host in dev.
# --------------------------------------------------------

_VEC_USER = os.getenv("PGVECTOR_USER", _DB_USER)
_VEC_HOST = os.getenv("PGVECTOR_HOST", _DB_HOST)
_VEC_PORT = os.getenv("PGVECTOR_PORT", _DB_PORT)
_VEC_NAME = os.getenv("PGVECTOR_DB",   _DB_NAME)

vector_engine = create_engine(
    URL.create(
        drivername="postgresql+psycopg2",
        username=_VEC_USER,
        password=_cfg("PGVECTOR_PASSWORD") or _cfg("POSTGRES_PASSWORD", "postgres"),
        host=_VEC_HOST,
        port=int(_VEC_PORT),
        database=_VEC_NAME,
        query={"options": f"-csearch_path={DB_SCHEMA},public"},
    ),
    pool_pre_ping=True,
    pool_size=30,              # pgvector queries are short; higher concurrency
    max_overflow=5,
    pool_timeout=30,
    pool_recycle=1800,
    connect_args=_CONNECT_ARGS,
    echo=False,
)

VectorSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=vector_engine)


# --------------------------------------------------------
# CQRS Read Replicas
# ReadSessionLocal    → PGS01 hot-standby (all SELECT queries)
# VectorReadSessionLocal → PGS02 hot-standby (pgvector HNSW searches)
#
# Both fall back to the write primary when the read-replica env vars are
# not set (i.e. same host/port as primary) — no behaviour change in dev.
# In prod: set POSTGRES_READ_HOST and PGVECTOR_READ_HOST in .env to route
# reads to the hot-standby, keeping the write primary free for writes only.
# --------------------------------------------------------

_RD_USER = os.getenv("POSTGRES_READ_USER",  _DB_USER)
_RD_HOST = os.getenv("POSTGRES_READ_HOST",  _DB_HOST)
_RD_PORT = os.getenv("POSTGRES_READ_PORT",  _DB_PORT)
_RD_NAME = os.getenv("POSTGRES_READ_DB",    _DB_NAME)

read_engine = create_engine(
    URL.create(
        drivername="postgresql+psycopg2",
        username=_RD_USER,
        password=_cfg("POSTGRES_READ_PASSWORD") or _cfg("POSTGRES_PASSWORD", "postgres"),
        host=_RD_HOST,
        port=int(_RD_PORT),
        database=_RD_NAME,
        query={"options": f"-csearch_path={DB_SCHEMA},public"},
    ),
    pool_pre_ping=True,
    pool_size=30,               # read workload is higher concurrency
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    connect_args=_CONNECT_ARGS,
    echo=False,
)

ReadSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=read_engine)

_VRD_USER = os.getenv("PGVECTOR_READ_USER",  _VEC_USER)
_VRD_HOST = os.getenv("PGVECTOR_READ_HOST",  _VEC_HOST)
_VRD_PORT = os.getenv("PGVECTOR_READ_PORT",  _VEC_PORT)

vector_read_engine = create_engine(
    URL.create(
        drivername="postgresql+psycopg2",
        username=_VRD_USER,
        password=_cfg("PGVECTOR_READ_PASSWORD") or _cfg("PGVECTOR_PASSWORD") or _cfg("POSTGRES_PASSWORD", "postgres"),
        host=_VRD_HOST,
        port=int(_VRD_PORT),
        database=_VEC_NAME,
        query={"options": f"-csearch_path={DB_SCHEMA},public"},
    ),
    pool_pre_ping=True,
    pool_size=40,               # pgvector reads are the hottest path
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    connect_args=_CONNECT_ARGS,
    echo=False,
)

VectorReadSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=vector_read_engine)


# --------------------------------------------------------
# Base class for ORM models
# All ORM models inherit this Base; SQLAlchemy will explicitly
# qualify table names as ainxt.<table> in all generated DDL and DML.
# --------------------------------------------------------

class Base(DeclarativeBase):
    metadata = MetaData(schema=DB_SCHEMA)


# --------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------

def get_db():
    """Yield a main Postgres session (PGS01); close on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def set_rls_context(conn, user: dict) -> None:
    """
    Set per-request RLS session variables so Row-Level Security policies
    can scope queries to the requesting user.

    Call this at the start of every request that touches multi-tenant tables:
        with engine.connect() as conn:
            set_rls_context(conn, current_user)
            ...

    The 'true' flag makes each setting LOCAL to the current transaction only.
    This is correct for PgBouncer transaction-mode pools — the variables are
    automatically cleared when the transaction ends, preventing cross-user leaks.

    Variables set:
        ainxt.current_user_id    — UUID of the requesting user
        ainxt.current_user_role  — role string (admin/operator/developer/etc.)
        ainxt.current_band_level — numeric band level (1=A1 ... 9=E)
    """
    from sqlalchemy import text as _text
    user_id    = str(user.get("sub", "") or user.get("id", ""))
    user_role  = str(user.get("role", "developer"))
    band_level = str(user.get("band_level", 1))
    conn.execute(_text(
        "SELECT "
        "  set_config('ainxt.current_user_id',    :uid,   TRUE), "
        "  set_config('ainxt.current_user_role',  :role,  TRUE), "
        "  set_config('ainxt.current_band_level', :band,  TRUE)"
    ), {"uid": user_id, "role": user_role, "band": band_level})


def get_read_db():
    """Yield a read-replica session (PGS01-RO). Falls back to primary if no replica configured."""
    db = ReadSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_vector_db():
    """Yield a pgVector write session (PGS02); close on exit."""
    db = VectorSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_vector_read_db():
    """Yield a pgVector read-replica session (PGS02-RO). Falls back to primary if no replica configured."""
    db = VectorReadSessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------
# Shared raw-connection accessor (single pool for the platform)
# --------------------------------------------------------

@contextmanager
def pg_raw_connection():
    """Borrow a raw psycopg2 connection from the shared SQLAlchemy engine pool.

    The single entry point for non-ORM code (e.g. ABStudio's psycopg-style
    repositories) so the whole process shares ONE pool. ``engine.raw_connection()``
    checks a connection out of the pool; ``.close()`` returns it (the socket
    stays open). The engine pins ``search_path=ainxt,public``, so unqualified
    names resolve to the ``ainxt`` schema exactly as ORM sessions do. Commits on
    clean exit, rolls back on exception.

    Usage::

        with pg_raw_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
    """
    raw = engine.raw_connection()
    try:
        yield raw
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
