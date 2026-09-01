# SPDX-License-Identifier: Apache-2.0
import os
import re
from typing import Optional

import httpx

from core.logger import logger

_ENABLED = os.getenv("KB_REPLICA_ENABLED", "false").lower() in ("1", "true", "yes", "on")
_PEER_URL = os.getenv("KB_REPLICA_PEER_URL", "").rstrip("/")
_STRICT = os.getenv("KB_REPLICA_STRICT", "false").lower() in ("1", "true", "yes", "on")
_NODE_ID = os.getenv("AINXT_NODE_ID", "unknown")
_TIMEOUT = float(os.getenv("KB_REPLICA_TIMEOUT_SECONDS", "60"))
_SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")


def is_enabled() -> bool:
    return _ENABLED and bool(_PEER_URL)


def _raise_or_warn(message: str, exc: Optional[Exception] = None) -> None:
    if _STRICT:
        raise RuntimeError(message) from exc
    logger.warning(message)


def replicate_file(doc_id: str, ext: str, data: bytes, *, kind: str = "file") -> bool:
    if not is_enabled():
        return False

    _ext = (ext or "").strip().lower().lstrip(".")
    if not doc_id or not _SAFE_EXT_RE.fullmatch(_ext):
        _raise_or_warn(f"kb_replication: invalid replicate request doc_id={doc_id!r} ext={ext!r}")
        return False

    try:
        resp = httpx.post(
            f"{_PEER_URL}/ainxt/v2/api/kb/internal/replicate-file",
            data={"doc_id": doc_id, "ext": _ext, "kind": kind, "source_node": _NODE_ID},
            files={"file": (f"{doc_id}.{_ext}", data, "application/octet-stream")},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        logger.info(
            f"kb_replication: replicated {kind} doc_id={doc_id} ext={_ext} "
            f"bytes={len(data):,} peer={_PEER_URL} source_node={_NODE_ID}"
        )
        return True
    except Exception as exc:
        _raise_or_warn(
            f"kb_replication: failed to replicate {kind} doc_id={doc_id} "
            f"ext={_ext} peer={_PEER_URL} error='{exc}'",
            exc,
        )
        return False


def delete_file(doc_id: str, ext: str, *, kind: str = "file") -> bool:
    if not is_enabled():
        return False

    _ext = (ext or "").strip().lower().lstrip(".")
    if not doc_id or not _SAFE_EXT_RE.fullmatch(_ext):
        logger.warning(f"kb_replication: invalid delete request doc_id={doc_id!r} ext={ext!r}")
        return False

    try:
        resp = httpx.post(
            f"{_PEER_URL}/ainxt/v2/api/kb/internal/delete-replica-file",
            json={"doc_id": doc_id, "ext": _ext, "kind": kind, "source_node": _NODE_ID},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        logger.info(
            f"kb_replication: deleted replica {kind} doc_id={doc_id} "
            f"ext={_ext} peer={_PEER_URL} source_node={_NODE_ID}"
        )
        return True
    except Exception as exc:
        logger.warning(
            f"kb_replication: failed to delete replica {kind} doc_id={doc_id} "
            f"ext={_ext} peer={_PEER_URL} error='{exc}'"
        )
        return False
