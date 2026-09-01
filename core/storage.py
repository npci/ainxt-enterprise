# SPDX-License-Identifier: Apache-2.0
# ============================================================
# OBJECT STORAGE — MinIO (primary) + local disk (fallback)
#
# Configuration via environment variables:
#   STORAGE_BACKEND=minio|local  (default: auto-detect)
#   MINIO_ENDPOINT=localhost:9000
#   MINIO_ACCESS_KEY=minioadmin
#   MINIO_SECRET_KEY=minioadmin
#   MINIO_BUCKET=chat-attachments
#   MINIO_SECURE=false
#
# Usage:
#   from core.storage import storage
#   path = await storage.save(file_bytes, filename, content_type)
#   data = await storage.load(path)
#   url  = storage.presigned_url(path, expires=3600)
#   await storage.delete(path)
# ============================================================

import os
import io
import re
import uuid
from pathlib import Path
from typing import Optional, Tuple

from core.logger import logger

_BUCKET       = os.getenv("MINIO_BUCKET",     "chat-attachments")
# No hardcoded localhost default — an unset/invalid endpoint fails
# _make_minio_client() below, which already falls back to local disk storage.
_ENDPOINT     = os.getenv("MINIO_ENDPOINT",   "")
_ACCESS_KEY   = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
_SECRET_KEY   = os.getenv("MINIO_SECRET_KEY", "minioadmin")
_SECURE       = os.getenv("MINIO_SECURE",     "false").lower() == "true"
_BACKEND      = os.getenv("STORAGE_BACKEND",  "auto")   # auto|minio|local

# Local disk layout. Prefer the per-asset-type env vars used by the rest of the
# app (AINXT_UPLOAD_*_PATH) so uploaded images/documents land where operators
# expect. Fall back to the legacy LOCAL_STORAGE_DIR only when those are unset.
_UPLOAD_IMAGE_DIR    = os.getenv("AINXT_UPLOAD_IMAGE_PATH")
_UPLOAD_DOCUMENT_DIR = os.getenv("AINXT_UPLOAD_DOCUMENT_PATH")
_LEGACY_LOCAL_DIR    = os.getenv("LOCAL_STORAGE_DIR", "storage/chat_attachments")

# ── Public subdir constants — used by upload callers to route into the
# correct SEPARATE tree (uploaded assets vs. generated docs/images).
UPLOAD_SUBDIR_IMAGE    = "uploads/images"
UPLOAD_SUBDIR_DOCUMENT = "uploads/documents"

# Fallback segment names when caller can't supply user_id / chat_id. Kept as
# module constants so config.py's _upload_dir and storage sharding stay in sync.
ANON_USER_SEGMENT = "unknown"
NO_CHAT_SEGMENT   = "no-chat"

# Path sanitisers. Two variants because `subdir` may be nested (allows `/`)
# whereas a single shard segment must NOT contain a path separator — that's
# what prevents shard-injection via a value like "a/../../etc".
_SUBDIR_RE  = re.compile(r"[^A-Za-z0-9_./-]")
_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_segment(value: str, fallback: str) -> str:
    """Sanitise a single path segment (no `/` allowed)."""
    s = _SEGMENT_RE.sub("_", (value or "").strip()).replace("..", "_")
    return s or fallback


def _local_base_for_subdir(subdir: str) -> Tuple[Path, str]:
    """Return (base_dir, remaining_subdir) for a caller-supplied subdir.

    Callers pass subdir="uploads/images" or "uploads/documents". When the
    matching AINXT_UPLOAD_*_PATH env var is set, that path already contains
    the terminal directory (e.g. .../uploads/images), so we use it as the
    base and strip the matching prefix from subdir. The final local path
    becomes exactly /appdata/ainxt/uploads/images/<id>.jpeg.

    If the env var is unset, fall back to LOCAL_STORAGE_DIR / <subdir> for
    backwards compatibility with existing deployments.
    """
    sub = (subdir or "").strip("/")
    if sub.startswith("uploads/images") and _UPLOAD_IMAGE_DIR:
        rest = sub[len("uploads/images"):].strip("/")
        return Path(_UPLOAD_IMAGE_DIR), rest
    if sub.startswith("uploads/documents") and _UPLOAD_DOCUMENT_DIR:
        rest = sub[len("uploads/documents"):].strip("/")
        return Path(_UPLOAD_DOCUMENT_DIR), rest
    return Path(_LEGACY_LOCAL_DIR), sub


def _make_minio_client():
    """Return a configured Minio client or None on failure."""
    try:
        from minio import Minio
        from minio.error import S3Error
        client = Minio(
            _ENDPOINT,
            access_key=_ACCESS_KEY,
            secret_key=_SECRET_KEY,
            secure=_SECURE,
        )
        # Ensure bucket exists
        if not client.bucket_exists(_BUCKET):
            client.make_bucket(_BUCKET)
            logger.info(f"Storage: created MinIO bucket '{_BUCKET}'")
        logger.info(f"Storage: MinIO connected at {_ENDPOINT}, bucket={_BUCKET}")
        return client
    except Exception as e:
        logger.info(f"Storage: MinIO not available ({e.__class__.__name__}) — using local disk")
        return None


class ObjectStorage:
    """
    Unified object storage. Uses MinIO when available; local disk otherwise.
    All paths returned are opaque strings — pass them back to load()/delete().
    """

    def __init__(self):
        self._client = None
        self._use_minio = False
        self._init()

    def _init(self):
        if _BACKEND == "local":
            logger.info("Storage: forced local disk mode")
            Path(_LEGACY_LOCAL_DIR).mkdir(parents=True, exist_ok=True)
            return

        self._client = _make_minio_client()
        self._use_minio = self._client is not None

        if not self._use_minio:
            Path(_LEGACY_LOCAL_DIR).mkdir(parents=True, exist_ok=True)

    # ── Public API ───────────────────────────────────────────

    def save(
        self,
        data: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
        subdir: str = "",
        user_id: str = "",
        chat_id: str = "",
    ) -> str:
        """
        Persist bytes and return an opaque path string.

        Layout: <subdir>/<user_id>/<chat_id>/<uuid>.<ext>

        When both user_id and chat_id are empty AND subdir is empty, the
        legacy flat layout is used so non-upload callers (generated
        docs/images) are unaffected.

        Returns:
            MinIO      → "minio:<object_name>"
            Local disk → absolute filesystem path
        """
        ext       = Path(filename).suffix.lstrip(".")
        object_id = f"{uuid.uuid4()}.{ext}" if ext else str(uuid.uuid4())

        sub = _SUBDIR_RE.sub("_", (subdir or "").strip("/")).replace("..", "_")

        shard = ""
        if sub or user_id or chat_id:
            uid = _safe_segment(user_id, ANON_USER_SEGMENT)
            cid = _safe_segment(chat_id, NO_CHAT_SEGMENT)
            shard = f"{uid}/{cid}"

        parts = [p for p in (sub, shard) if p]
        object_name = f"{'/'.join(parts)}/{object_id}" if parts else object_id

        if self._use_minio and self._client:
            try:
                self._client.put_object(
                    _BUCKET, object_name,
                    io.BytesIO(data), len(data),
                    content_type=content_type,
                )
                logger.info(
                    f"Storage: saved {object_name} to MinIO ({len(data)} bytes) "
                    f"user={user_id or '-'} chat={chat_id or '-'}"
                )
                return f"minio:{object_name}"
            except Exception as e:
                logger.warning(f"Storage: MinIO put failed ({e}), falling back to local")

        local_base, local_sub = _local_base_for_subdir(subdir)
        if shard:
            local_sub = f"{local_sub}/{shard}" if local_sub else shard
        local_dir = (local_base / local_sub) if local_sub else local_base
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / object_id
        local_path.write_bytes(data)
        logger.info(
            f"Storage: saved {object_name} to local disk ({len(data)} bytes) "
            f"user={user_id or '-'} chat={chat_id or '-'}"
        )
        return str(local_path)

    def load(self, path: str) -> Optional[bytes]:
        """Load bytes from a path returned by save()."""
        if path.startswith("minio:") and self._use_minio and self._client:
            object_id = path[len("minio:"):]
            try:
                resp = self._client.get_object(_BUCKET, object_id)
                return resp.read()
            except Exception as e:
                logger.error(f"Storage: MinIO get failed for {object_id}: {e}")
                return None

        # Local
        local_path = path[len("local:"):] if path.startswith("local:") else path
        p = Path(local_path)
        if p.exists():
            return p.read_bytes()
        logger.error(f"Storage: local file not found: {local_path}")
        return None

    def delete(self, path: str) -> bool:
        """Delete an object. Returns True on success."""
        if path.startswith("minio:") and self._use_minio and self._client:
            object_id = path[len("minio:"):]
            try:
                self._client.remove_object(_BUCKET, object_id)
                return True
            except Exception as e:
                logger.warning(f"Storage: MinIO delete failed for {object_id}: {e}")
                return False

        local_path = path[len("local:"):] if path.startswith("local:") else path
        p = Path(local_path)
        if p.exists():
            p.unlink()
            return True
        return False

    def presigned_url(self, path: str, expires: int = 3600) -> Optional[str]:
        """Return a presigned URL for direct download (MinIO only). None for local."""
        if path.startswith("minio:") and self._use_minio and self._client:
            from datetime import timedelta
            object_id = path[len("minio:"):]
            try:
                url = self._client.presigned_get_object(
                    _BUCKET, object_id,
                    expires=timedelta(seconds=expires),
                )
                return url
            except Exception as e:
                logger.warning(f"Storage: presigned URL failed: {e}")
        return None

    @property
    def backend(self) -> str:
        return "minio" if self._use_minio else "local"


# ── Singleton ────────────────────────────────────────────────
storage = ObjectStorage()
