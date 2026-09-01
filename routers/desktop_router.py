# SPDX-License-Identifier: Apache-2.0
"""
Desktop Agent Router — /desktop/*

Handles local file indexing and local MCP server registration
from the AiNxt Electron desktop app.

Endpoints:
  POST /desktop/index/file          — index a single local file inline
  POST /desktop/index/batch         — queue a batch of files as a background job
  DELETE /desktop/index/{workspace} — remove all vectors for a local workspace
  GET  /desktop/index/{workspace}/status — indexing progress
  POST /desktop/register-mcp        — register the local MCP server with AiNxt
  GET  /desktop/mcp/tools           — list tools from registered local MCP server
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.logger import logger
from core.config import POSTGRES_DB as _cfg_postgres_db
from core.config import POSTGRES_HOST as _cfg_postgres_host

router = APIRouter(prefix="/ainxt/v1/api/desktop", tags=["desktop"])

# MCP registry backed by the workflow KV (DB=2, transient state) —
# shared across all uvicorn workers. Backend (Redis)
# is selected via REDIS_CLIENT_CONFIG_DB2.
_MCP_TTL = 28_800  # 8 hours

def _mcp_key(user_id: str) -> str:
    return f"desktop:mcp:{user_id}"

def _mcp_kv():
    from core.config import RDB_WORKFLOW
    from core.kv import get_kv
    return get_kv(RDB_WORKFLOW, decode_responses=True)

def _mcp_get(user_id: str) -> dict | None:
    try:
        raw = _mcp_kv().get(_mcp_key(user_id))
        return json.loads(raw) if raw else None
    except Exception:
        return None

def _mcp_set(user_id: str, entry: dict) -> None:
    try:
        _mcp_kv().set(_mcp_key(user_id), json.dumps(entry), ex=_MCP_TTL)
    except Exception:
        pass

def _mcp_del(user_id: str) -> None:
    try:
        _mcp_kv().delete(_mcp_key(user_id))
    except Exception:
        pass

DESKTOP_REPO_PREFIX = "desktop"
MAX_FILE_SIZE       = 524_288   # 512 KB


# ── Request models ─────────────────────────────────────────────────────────────

class IndexFileRequest(BaseModel):
    workspace: str          # e.g. "my-project" — becomes repo "desktop:my-project"
    filename: str           # relative path from workspace root
    content: str            # raw file content (UTF-8)
    language: Optional[str] = None  # auto-detected if omitted

class IndexBatchRequest(BaseModel):
    workspace: str
    files: list[dict]       # [{filename, content, language?}]

class RegisterMcpRequest(BaseModel):
    port: int
    tools: list  # list of tool objects {name, description, inputSchema} or plain strings


# ── File indexing ──────────────────────────────────────────────────────────────

@router.post("/index/file")
def index_file(body: IndexFileRequest, current_user=Depends(get_current_user)):
    """
    Index a single local file inline (no job queue).
    Suitable for incremental watcher-driven re-indexing.
    """
    if len(body.content) > MAX_FILE_SIZE:
        raise HTTPException(400, "File content exceeds 512 KB limit")

    repo_name = f"{DESKTOP_REPO_PREFIX}_{body.workspace.replace('-', '_').replace(' ', '_')}"
    lang = body.language or _detect_language(body.filename)
    start = time.time()

    try:
        chunks, symbols = _chunk_content(body.content, body.filename, lang)
        if not chunks:
            return {"indexed": 0, "workspace": body.workspace, "filename": body.filename}

        texts     = [c["content"] for c in chunks]
        embeddings = _embed_texts(texts)

        _upsert_chunks(repo_name, chunks, embeddings,
                       dept=_user_dept(current_user))

        elapsed_ms = int((time.time() - start) * 1000)
        logger.info(
            f"desktop: indexed {len(chunks)} chunks from {body.filename} "
            f"into {repo_name} ({elapsed_ms}ms)"
        )
        return {
            "indexed": len(chunks),
            "workspace": body.workspace,
            "repo": repo_name,
            "filename": body.filename,
            "latency_ms": elapsed_ms,
        }
    except Exception as e:
        logger.error(f"desktop: index_file failed — {e}")
        raise HTTPException(500, f"Indexing failed: {e}")


@router.post("/index/batch")
def index_batch(body: IndexBatchRequest, current_user=Depends(get_current_user)):
    """
    Index multiple local files via RQ background job.
    Returns job_id for SSE polling.
    """
    if len(body.files) > 500:
        raise HTTPException(400, "Batch limited to 500 files per request")

    from core.job_queue import get_queue
    q = get_queue("index_queue")
    job = q.enqueue(
        "workers.desktop_index_worker.index_local_batch",
        {
            "workspace": body.workspace,
            "files": body.files,
            "user_id": current_user["sub"],
            "dept": _user_dept(current_user),
        },
        job_timeout=300,
        retry=__import__("rq").Retry(max=2, interval=[10, 30]),
    )
    return {"job_id": job.id, "workspace": body.workspace, "file_count": len(body.files)}


@router.delete("/index/{workspace}")
def delete_workspace(workspace: str, current_user=Depends(get_current_user)):
    """Remove all vectors for a local workspace."""
    repo_name = f"{DESKTOP_REPO_PREFIX}_{workspace.replace('-', '_').replace(' ', '_')}"
    try:
        import psycopg2
        conn = _get_vec_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM document_embeddings WHERE repo = %s",
                    (repo_name,)
                )
                deleted = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return {"deleted_chunks": deleted, "workspace": workspace, "repo": repo_name}
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {e}")


@router.get("/index/{workspace}/status")
def workspace_status(workspace: str, current_user=Depends(get_current_user)):
    """Return chunk count and last-indexed time for a local workspace."""
    repo_name = f"{DESKTOP_REPO_PREFIX}_{workspace.replace('-', '_').replace(' ', '_')}"
    try:
        conn = _get_vec_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), MAX(created_at) FROM document_embeddings WHERE repo = %s",
                    (repo_name,)
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return {
            "workspace": workspace,
            "repo": repo_name,
            "chunk_count": row[0] if row else 0,
            "last_indexed": row[1].isoformat() if row and row[1] else None,
        }
    except Exception as e:
        raise HTTPException(500, f"Status check failed: {e}")


# ── Local MCP server registration ──────────────────────────────────────────────

@router.post("/register-mcp")
def register_mcp(body: RegisterMcpRequest, current_user=Depends(get_current_user)):
    """
    Register the desktop's local MCP HTTP server with AiNxt.
    Verifies the server is reachable before accepting.
    """
    user_id = current_user["sub"]

    # Verify the local MCP server is actually reachable.  The desktop app serves
    # its loopback MCP server over HTTPS with an ephemeral self-signed cert that
    # is bound to 127.0.0.1, so certificate verification is disabled for this
    # localhost-only health check (CWE-319 mitigation).
    try:
        resp = httpx.get(
            f"https://127.0.0.1:{body.port}/tools",
            timeout=3,
            verify=False,
        )
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(
            400,
            f"Cannot reach local MCP server on port {body.port}: {e}. "
            "Make sure the desktop app is running."
        )

    _mcp_set(user_id, {
        "port": body.port,
        "tools": body.tools,
        "registered_at": int(time.time()),
        "base_url": f"https://127.0.0.1:{body.port}",
    })
    logger.info(f"desktop: registered local MCP server for user {user_id} on port {body.port}")
    return {"registered": True, "port": body.port, "tools": body.tools}


@router.get("/mcp/tools")
def list_mcp_tools(current_user=Depends(get_current_user)):
    """Return tools from this user's registered local MCP server."""
    user_id = current_user["sub"]
    entry   = _mcp_get(user_id)
    if not entry:
        return {"tools": [], "registered": False}

    try:
        resp = httpx.get(f"{entry['base_url']}/tools", timeout=3)
        resp.raise_for_status()
        return {"tools": resp.json().get("tools", []), "registered": True, "port": entry["port"]}
    except Exception as e:
        return {"tools": [], "registered": False, "error": str(e)}


def execute_local_mcp_tool(user_id: str, tool: str, input_params: dict) -> dict:
    """
    Called by the orchestrator to execute a local MCP tool for a user.
    Returns the tool result or raises RuntimeError.
    """
    entry = _mcp_get(user_id)
    if not entry:
        raise RuntimeError(
            "Local MCP server is not registered. Open the AiNxt desktop app first."
        )

    try:
        resp = httpx.post(
            f"{entry['base_url']}/execute",
            json={"tool": tool, "input": input_params},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"Local MCP tool '{tool}' failed: {e}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _detect_language(filename: str) -> str:
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "tsx", ".jsx": "jsx", ".java": "java", ".kt": "kotlin",
        ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
        ".cs": "c_sharp", ".cpp": "cpp", ".cc": "cpp", ".c": "c",
        ".swift": "swift", ".scala": "scala", ".sh": "bash",
        ".sql": "sql", ".md": "markdown", ".yaml": "yaml", ".yml": "yaml",
        ".json": "json",
    }
    import os
    return ext_map.get(os.path.splitext(filename)[1].lower(), "text")


def _chunk_content(content: str, filename: str, language: str) -> tuple[list[dict], list[dict]]:
    """Reuse index_worker chunking logic for a single file."""
    try:
        from workers.index_worker import _ts_chunk, _line_chunk
        chunks, symbols = _ts_chunk(content, filename, language)
        if not chunks:
            chunks = list(_line_chunk(content, filename))
            symbols = []
        return chunks, symbols
    except Exception as e:
        logger.warning(f"desktop: chunking failed ({e}), falling back to line chunking")
        from workers.index_worker import _line_chunk, _make_chunk
        return list(_line_chunk(content, filename)), []


def _embed_texts(texts: list[str]) -> list[list[float]]:
    from workers.index_worker import _call_embed_svc
    return _call_embed_svc(texts)


def _upsert_chunks(repo_name: str, chunks: list[dict], embeddings: list[list[float]], dept: str = "") -> None:
    from workers.index_worker import _bulk_upsert
    _bulk_upsert(repo_name, chunks, embeddings, department=dept)


def _user_dept(current_user: dict) -> str:
    return current_user.get("department", "")


def _get_vec_pg():
    import psycopg2
    from core.config import _cfg
    # No localhost default: reuses the canonical (also no-default) core.config value.
    vec_host = os.getenv("VECTOR_POSTGRES_HOST") or os.getenv("POSTGRES_HOST", _cfg_postgres_host)
    vec_port = int(os.getenv("VECTOR_POSTGRES_PORT") or os.getenv("POSTGRES_PORT", 5432))
    _params = {
        "host":            vec_host,
        "port":            vec_port,
        "dbname":          os.getenv("POSTGRES_DB", _cfg_postgres_db),
        "user":            os.getenv("POSTGRES_USER", "admin"),
        "connect_timeout": 5,
    }
    _params.update({"password": _cfg("POSTGRES_PASSWORD")})
    return psycopg2.connect(**_params)
