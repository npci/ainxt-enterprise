# SPDX-License-Identifier: Apache-2.0
"""
AiNxt Agentic Platform — kb_search MCP tools.

Namespaced keyword retrieval over configured corpora. Used by UC-59
(KB-grounded reply drafting), UC-66 (HR policy Q&A), and UC-93 (RFP content
library). Swap provider to pgvector/elastic in production — the tool
contract stays identical.

Functions exposed:
  list_namespaces  — list configured KB namespaces + ACL band
  search           — keyword search a namespace; returns scored passages
  get_document     — fetch the full text of a document by id

Companion server: mcp/servers/kb_search_server.py
Registered in:   mcp/registry.py:_register_tools()

Configuration (env vars, all optional):
  KB_SEARCH_DATA_DIR       — root directory holding the corpora (default ./data/kb)
  KB_SEARCH_NAMESPACES     — JSON dict of {namespace_name: subdir} (default "{}")
  KB_SEARCH_ACL_BAND       — string tag returned by list_namespaces (default INTERNAL)
  KB_SEARCH_PROVIDER       — backend hint, informational only (default local_keyword)
  KB_SEARCH_DEFAULT_TOP_K  — fallback top_k when caller passes 0 (default 3)
"""

import json
import os
import re
from typing import Dict, List

try:
    from pypdf import PdfReader
except ImportError:  # pypdf is in requirements.txt but be defensive
    PdfReader = None  # type: ignore[assignment]


# ── Configuration ────────────────────────────────────────────────────────────

_DATA_DIR      = os.getenv("KB_SEARCH_DATA_DIR", "./data/kb")
_NAMESPACES    = json.loads(os.getenv("KB_SEARCH_NAMESPACES") or "{}")
_ACL_BAND      = os.getenv("KB_SEARCH_ACL_BAND", "INTERNAL")
_PROVIDER      = os.getenv("KB_SEARCH_PROVIDER", "local_keyword")
_DEFAULT_TOP_K = int(os.getenv("KB_SEARCH_DEFAULT_TOP_K", "3"))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    if path.lower().endswith(".pdf"):
        if PdfReader is None:
            raise RuntimeError("pypdf is required to read PDF files")
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    return open(path, encoding="utf-8", errors="replace").read()


def _corpus(namespace: str) -> List[Dict]:
    sub = _NAMESPACES.get(namespace)
    if not sub:
        raise ValueError(
            f"Unknown namespace '{namespace}'. Known: {list(_NAMESPACES)}"
        )
    root = os.path.join(_DATA_DIR, sub)
    docs: List[Dict] = []
    for r, _, files in os.walk(root):
        for f in files:
            if f.lower().endswith((".pdf", ".md", ".txt")):
                p = os.path.join(r, f)
                docs.append({"doc_id": os.path.relpath(p, root), "text": _read(p)})
    return docs


# ── Tool functions ───────────────────────────────────────────────────────────

def list_namespaces() -> dict:
    """List configured KB namespaces and their ACL band."""
    return {"namespaces": list(_NAMESPACES), "acl_band": _ACL_BAND}


def search(namespace: str, query: str, top_k: int = 0) -> list:
    """Search a KB namespace for passages relevant to the query.
    Returns scored passages with source doc ids for citation.
    """
    top_k = top_k or _DEFAULT_TOP_K
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    results = []
    for doc in _corpus(namespace):
        paras = [p.strip() for p in re.split(r"\n\s*\n", doc["text"]) if len(p.strip()) > 40]
        for para in paras:
            pl = para.lower()
            score = sum(pl.count(t) for t in terms)
            if score:
                results.append({"doc_id": doc["doc_id"], "score": score, "passage": para[:900]})
    results.sort(key=lambda x: -x["score"])
    return results[:top_k]


def get_document(namespace: str, doc_id: str, max_chars: int = 20000) -> dict:
    """Fetch the full text of a specific document in a namespace by its doc_id."""
    for doc in _corpus(namespace):
        if doc["doc_id"] == doc_id:
            return {"doc_id": doc_id, "text": doc["text"][:max_chars]}
    raise FileNotFoundError(doc_id)
