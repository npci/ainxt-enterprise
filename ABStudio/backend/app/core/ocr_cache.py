# SPDX-License-Identifier: Apache-2.0
"""Content-hash OCR result cache.

Re-uploading the same document (same bytes) currently re-runs OCR end to
end. For PDFs of any size this is the dominant latency in Build Studio.
The cache keys results by SHA-256 of the raw file bytes, scoped per OCR
options so different `force_ocr`/`describe_visuals`/`ocr_lang` combos
each get their own cached payload.

Storage: one JSON file per (sha, options-hash) under
``backend/runtime_artifacts/ocr_cache/``. Bounded by a simple LRU sweep
(touch ``atime`` on read; evict by oldest ``atime`` when the cache
exceeds ``_MAX_ENTRIES``). Pure stdlib — no Redis, no SQLite, no extra
deps.

The cache is **best-effort**: every read/write is wrapped in a broad
``except`` and a miss simply re-runs OCR. The pipeline never hard-fails
because of a cache problem.
"""
from __future__ import annotations

import hashlib
import json

import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.logger import logger
# Cache directory — colocated with other ABStudio runtime artifacts.
_CACHE_DIR = (
    Path(__file__).resolve().parent.parent.parent  # backend/
    / "runtime_artifacts"
    / "ocr_cache"
)

# Cap is generous enough that day-to-day Build Studio use never evicts,
# but small enough to keep disk + listdir-scan O(n) cheap.
_MAX_ENTRIES = 500


def _ensure_dir() -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover — host-specific
        logger.debug(f'[AGENT] ocr_cache: cannot create {_CACHE_DIR}: {exc}')


def _content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _options_fingerprint(options: Dict[str, Any]) -> str:
    """Stable hash of the OCR options that influence output.

    Only options that change the produced text/structure are mixed in.
    UI-only options (e.g., progress reporting) must not be passed here.
    """
    canonical = json.dumps(options, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _entry_path(sha: str, opt_fp: str) -> Path:
    return _CACHE_DIR / f"{sha}_{opt_fp}.json"


def get(content: bytes, options: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the cached extraction payload or ``None`` on miss/error."""
    try:
        path = _entry_path(_content_hash(content), _options_fingerprint(options))
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        # Touch atime so LRU eviction keeps recently-used entries warm.
        try:
            os.utime(path, None)
        except OSError:
            pass
        return payload
    except Exception as exc:  # pragma: no cover — cache is best-effort
        logger.debug(f'[AGENT] ocr_cache: get failed: {exc}')
        return None


def put(content: bytes, options: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Persist ``payload`` for the given content + options. Best-effort."""
    try:
        _ensure_dir()
        sha = _content_hash(content)
        opt_fp = _options_fingerprint(options)
        target = _entry_path(sha, opt_fp)

        # Atomic-ish write: write to a tempfile in the cache dir then rename.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{sha[:8]}_", suffix=".json", dir=str(_CACHE_DIR),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_name, target)
        except Exception:
            # Clean up the partial tempfile and re-raise to outer handler.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        _maybe_evict()
    except Exception as exc:  # pragma: no cover — cache is best-effort
        logger.debug(f'[AGENT] ocr_cache: put failed: {exc}')


def _maybe_evict() -> None:
    """If the cache is over its cap, drop the oldest entries by atime."""
    try:
        if not _CACHE_DIR.exists():
            return
        entries = [
            p for p in _CACHE_DIR.iterdir()
            if p.is_file() and p.suffix == ".json"
        ]
        if len(entries) <= _MAX_ENTRIES:
            return
        # Oldest atime first — those go.
        entries.sort(key=lambda p: p.stat().st_atime)
        excess = len(entries) - _MAX_ENTRIES
        for p in entries[:excess]:
            try:
                p.unlink()
            except OSError:
                pass
    except Exception as exc:  # pragma: no cover — cache is best-effort
        logger.debug(f'[AGENT] ocr_cache: evict failed: {exc}')


def invalidate(content: bytes, options: Dict[str, Any]) -> bool:
    """Delete a single cache entry. Returns True iff a file was removed.

    Used by the pipeline when it detects a cached payload contains
    "<lib> not installed" warnings for libs that are now importable —
    those entries should be re-extracted from scratch rather than
    served as-is.
    """
    try:
        path = _entry_path(_content_hash(content), _options_fingerprint(options))
        if not path.exists():
            return False
        path.unlink()
        return True
    except Exception as exc:  # pragma: no cover — best-effort
        logger.debug(f'[AGENT] ocr_cache: invalidate failed: {exc}')
        return False


def clear() -> int:
    """Remove all cache entries. Returns the number of files deleted.

    Exposed for tests and for a future admin-only endpoint.
    """
    count = 0
    try:
        if not _CACHE_DIR.exists():
            return 0
        for p in _CACHE_DIR.iterdir():
            if p.is_file() and p.suffix == ".json":
                try:
                    p.unlink()
                    count += 1
                except OSError:
                    pass
    except Exception as exc:  # pragma: no cover
        logger.debug(f'[AGENT] ocr_cache: clear failed: {exc}')
    return count
