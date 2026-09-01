# SPDX-License-Identifier: Apache-2.0
# ============================================================
# JIRA INDEXER
# Fetches Jira issues and indexes them into pgvector
# (document_embeddings, repo = docs_kb:jira_{project_key})
# ============================================================

import os
import re
import json
import base64
import uuid
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

from core.logger import logger

# ============================================================
# CONFIGURATION
# ============================================================

COLLECTION_NAME = "jira_issues"   # kept for return-value compat
BATCH_SIZE      = 100        # max results per Jira API call (API hard limit)
MAX_TEXT_CHARS  = 2000       # truncation limit per document


# ============================================================
# HELPERS
# ============================================================

def _strip_html(raw: str) -> str:
    """Remove HTML / Jira wiki markup tags and normalise whitespace."""
    if not raw:
        return ""
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", raw)
    # Expand common HTML entities
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_description_text(description) -> str:
    """
    Jira API v3 returns description as an Atlassian Document Format (ADF) dict.
    We walk the tree and collect plain text leaf nodes.
    Falls back to str() if the field is already a plain string.
    """
    if not description:
        return ""

    if isinstance(description, str):
        return _strip_html(description)

    # ADF object
    if isinstance(description, dict):
        return _adf_to_text(description)

    return ""


def _adf_to_text(node: dict) -> str:
    """Recursively extract text from an Atlassian Document Format node."""
    parts = []
    node_type = node.get("type", "")

    # Leaf text node
    if node_type == "text":
        parts.append(node.get("text", ""))

    # Recurse into content array
    for child in node.get("content", []):
        parts.append(_adf_to_text(child))

    # Add spacing after block-level nodes
    if node_type in ("paragraph", "heading", "bulletList", "orderedList",
                     "listItem", "blockquote", "codeBlock", "rule"):
        parts.append(" ")

    return " ".join(p for p in parts if p).strip()


def _make_auth_header(email: str, token: str) -> str:
    """Build HTTP Basic Auth header value (used in local-dev direct path)."""
    credentials = f"{email}:{token}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def _make_opener():
    """Build a urllib opener for direct Jira calls, honoring enterprise forward proxies."""
    import ssl

    # TLS certificate verification is always enforced (CWE-599). When
    # REQUESTS_CA_BUNDLE / SSL_CERT_FILE names a bundle we verify against it;
    # otherwise we verify against the system trust store. There is deliberately
    # no unverified fallback — an environment without a usable trust anchor must
    # be provisioned with one rather than silently accepting forged certificates.
    _ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or ""
    ctx = ssl.create_default_context(cafile=_ca_bundle or None)

    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    forward_proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("FORWARD_PROXY_URL")
        or ""
    )
    if forward_proxy:
        handlers.insert(0, urllib.request.ProxyHandler({"https": forward_proxy, "http": forward_proxy}))

    return urllib.request.build_opener(*handlers)


def _fetch_json(path: str, email: str, token: str) -> dict:
    """
    Perform a Jira GET and return parsed JSON.

    Production (LLM_PROXY_URL set): routes through the LLM proxy server LLM proxy because
    Jira Cloud is not reachable from the gateway server directly.

    Local dev (LLM_PROXY_URL unset): calls Jira directly using JIRA_URL.
    The ``path`` argument is the API path, e.g. /rest/api/3/search?jql=...
    """
    proxy_url = os.environ.get("LLM_PROXY_URL", "").rstrip("/")
    if proxy_url:
        import httpx
        # ── Correlation ID propagation ─────────────────────────────────────────
        # Carry request_id / chat_id from the indexer job's thread-local context
        # into the llm_proxy so every Atlassian API call is traceable back to the
        # originating index job.
        _proxy_body: dict = {
            "service": "jira", "method": "GET", "path": path,
            "email": email, "token": token,
        }
        try:
            from core.logger import get_request_id, get_chat_id
            _rid = get_request_id()
            _cid = get_chat_id()
            if _rid and _rid != "-":
                _proxy_body["request_id"] = _rid
            if _cid and _cid != "-":
                _proxy_body["chat_id"] = _cid
        except Exception:
            pass
        # ──────────────────────────────────────────────────────────────────────
        from core.proxy_tool_use import llm_proxy_headers as _lph
        resp = httpx.post(
            f"{proxy_url}/atlassian/proxy",
            json=_proxy_body,
            headers=_lph(),
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()
    else:
        base_url = os.environ.get("JIRA_URL", "").rstrip("/")
        full_url = f"{base_url}{path}"
        req = urllib.request.Request(full_url, headers={
            "Authorization": _make_auth_header(email, token),
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        with _make_opener().open(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))


# ============================================================
# MAIN INDEXER
# ============================================================

def index_jira_project(project_key: str, max_issues: int = 500,
                       user_id: str = "", user_email: str = "") -> dict:
    """
    Fetch Jira issues for a project and index them into
    pgvector (document_embeddings, repo = docs_kb:jira_{project_key}).

    Parameters
    ----------
    project_key : str
        Jira project key (e.g. "AiNxt").
    max_issues : int
        Maximum number of issues to index (default 500).
    user_id : str
        Platform user ID — used to look up the caller's stored Atlassian token.
    user_email : str
        Platform user email — fallback for token lookup if user_id is not supplied.
    """

    # --------------------------------------------------------
    # Resolve credentials — user's stored Atlassian token only;
    # service-account credentials are never used.
    # --------------------------------------------------------
    base_url = os.environ.get("JIRA_URL", "").rstrip("/")

    if not base_url:
        msg = "JIRA_URL is not configured"
        logger.error(f"JiraIndexer: {msg}")
        return {"indexed": 0, "skipped": 0, "errors": 0, "collection": COLLECTION_NAME, "error": msg}

    if not project_key:
        msg = "project_key is required"
        logger.error(f"JiraIndexer: {msg}")
        return {"indexed": 0, "skipped": 0, "errors": 0, "collection": COLLECTION_NAME, "error": msg}

    try:
        from core.platform_credentials import get_atlassian_creds
        email, token = get_atlassian_creds(user_id=user_id, email=user_email)
    except PermissionError as exc:
        msg = str(exc)
        logger.error(f"JiraIndexer: {msg}")
        return {"indexed": 0, "skipped": 0, "errors": 0, "collection": COLLECTION_NAME, "error": msg}

    # --------------------------------------------------------
    # pgvector setup (embed svc + document_embeddings)
    # --------------------------------------------------------
    # No hardcoded localhost default — same env var as core.config.EMBED_SVC_URL.
    _EMBED_SVC = os.getenv("EMBED_SVC_URL", "")
    _repo_key  = f"docs_kb:jira_{project_key.lower()}"

    def _pgvector_upsert(texts, ids, metas):
        """Embed texts via embed svc and upsert into document_embeddings."""
        try:
            import httpx as _httpx
            resp = _httpx.post(f"{_EMBED_SVC}/embed", json={"texts": texts, "provider": "ollama"}, timeout=120.0)
            resp.raise_for_status()
            embeddings = resp.json()["embeddings"]
        except Exception as e:
            logger.error(f"JiraIndexer: embed svc failed: {e}")
            return 0

        from db.database import VectorSessionLocal
        from db.models import DocumentEmbedding
        vdb = VectorSessionLocal()
        try:
            for text, emb, doc_id, meta in zip(texts, embeddings, ids, metas):
                row = DocumentEmbedding(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"jira:{doc_id}")),
                    repo=_repo_key,
                    file_path=meta.get("key", doc_id),
                    chunk_index=0,
                    content=text,
                    embedding=emb,
                    metadata_=meta,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    classification="INTERNAL",
                    uploaded_by="jira-indexer",
                    created_at=datetime.utcnow(),
                )
                vdb.merge(row)
            vdb.commit()
            # Register namespace in the KV cache (DB=0)
            try:
                from core.config import RDB_CACHE
                from core.kv import get_kv
                _r = get_kv(RDB_CACHE, decode_responses=True)
                _r.sadd("docs:namespaces", f"jira_{project_key.lower()}")
            except Exception:
                pass
            return len(texts)
        except Exception as e:
            vdb.rollback()
            logger.error(f"JiraIndexer: pgvector insert failed: {e}")
            return 0
        finally:
            vdb.close()

    # --------------------------------------------------------
    # Pagination loop
    # --------------------------------------------------------
    indexed  = 0
    skipped  = 0
    errors   = 0
    start_at = 0

    fields = "summary,description,status,issuetype,priority,assignee"

    logger.info(f"JiraIndexer: starting index for project '{project_key}' (max={max_issues})")

    while indexed + skipped < max_issues:
        remaining     = max_issues - (indexed + skipped)
        batch_size    = min(BATCH_SIZE, remaining)
        jql           = urllib.parse.quote(f"project={project_key} ORDER BY created DESC")
        api_path = (
            f"/rest/api/3/search"
            f"?jql={jql}"
            f"&maxResults={batch_size}"
            f"&startAt={start_at}"
            f"&fields={urllib.parse.quote(fields)}"
        )

        try:
            data = _fetch_json(api_path, email, token)
        except urllib.error.HTTPError as e:
            logger.error(f"JiraIndexer: HTTP {e.code} fetching issues at startAt={start_at}: {e}")
            errors += 1
            break
        except Exception as e:
            logger.error(f"JiraIndexer: fetch failed at startAt={start_at}: {e}")
            errors += 1
            break

        issues = data.get("issues", [])
        if not issues:
            break

        # ------------------------------------------------
        # Process each issue
        # ------------------------------------------------
        doc_ids   = []
        doc_texts = []
        doc_metas = []

        for issue in issues:
            try:
                issue_key = issue.get("key", "")
                fields_data = issue.get("fields", {})

                summary     = fields_data.get("summary", "") or ""
                description = _extract_description_text(fields_data.get("description"))
                status      = (fields_data.get("status") or {}).get("name", "")
                issuetype   = (fields_data.get("issuetype") or {}).get("name", "")
                priority    = (fields_data.get("priority") or {}).get("name", "")
                assignee    = (fields_data.get("assignee") or {}).get("displayName", "Unassigned")

                # Combine summary + description
                combined = f"{summary}. {description}".strip(". ")

                if not combined:
                    skipped += 1
                    continue

                # Truncate
                text = (combined[:MAX_TEXT_CHARS] + "...") if len(combined) > MAX_TEXT_CHARS else combined

                # Structured prefix improves embedding quality
                doc_text = (
                    f"Project: {project_key}\n"
                    f"Issue: {issue_key}\n"
                    f"Type: {issuetype}\n"
                    f"Status: {status}\n"
                    f"Priority: {priority}\n"
                    f"Assignee: {assignee}\n\n"
                    f"{text}"
                )

                doc_ids.append(issue_key)
                doc_texts.append(doc_text)
                doc_metas.append({
                    "key":        issue_key,
                    "summary":    summary,
                    "status":     status,
                    "issuetype":  issuetype,
                    "priority":   priority,
                    "source":     "jira",
                    "project":    project_key,
                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                })

            except Exception as e:
                logger.error(f"JiraIndexer: error processing issue {issue.get('key')}: {e}")
                errors += 1

        # ------------------------------------------------
        # Batch upsert into pgvector
        # ------------------------------------------------
        if doc_ids:
            n = _pgvector_upsert(doc_texts, doc_ids, doc_metas)
            if n:
                indexed += n
                logger.info(f"JiraIndexer: indexed {n} issues (total so far: {indexed})")
            else:
                errors += len(doc_ids)

        # ------------------------------------------------
        # Pagination bookkeeping
        # ------------------------------------------------
        total    = data.get("total", 0)
        start_at += len(issues)

        if start_at >= total or len(issues) == 0:
            break

    logger.info(
        f"JiraIndexer: finished — indexed={indexed} skipped={skipped} errors={errors}"
    )
    return {
        "indexed":    indexed,
        "skipped":    skipped,
        "errors":     errors,
        "collection": COLLECTION_NAME,
    }
