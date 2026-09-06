# SPDX-License-Identifier: MIT
"""
COWORK document-SKILL worker.

Runs the office agent's document build (docx-js authored per the
platform SKILL.md) inside the isolated `ainxt-doc-sandbox` image and stores
the result so the EXISTING download flow works unchanged:
  - file  → DOC_DIR/{file_id}.{ext}   (served by GET /docs/download/{file_id})
  - result→ Redis  doc:result:{job_id} (polled by GET /docs/job/{job_id}/status)
Plus a page-image preview alongside (DOC_DIR/{file_id}.page-N.jpg), surfaced via
GET /docs/preview/{file_id}/{page} so the renderer can show the doc in-app.

Kept OFF the gateway process (RQ doc_queue) — the docker build/convert is slow.
"""
from __future__ import annotations

import json
import os

# Reuse the canonical doc storage helpers so downloads/audit behave identically.
from workers.doc_worker import (
    DOC_DIR, RESULT_TTL, _R, _fail, _save_audit, _uuid_mod, purge_expired_docs,
    _atomic_write_bytes, _PPTX_SHIM_JS, _publish_progress,
)
from core.config import user_doc_dir
from core.logger import logger


def _slug(title: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9 _-]", "", (title or "document")).strip().replace(" ", "_")
    return (s[:48] or "document").lower()


def _next_artifact_version(artifact_id: str) -> int:
    """Next version number for an artifact = max(existing) + 1 (>=2). Computed at
    insert time so rapid revisions of the same artifact don't collide on v1."""
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        from sqlalchemy import func
        db = SessionLocal()
        try:
            cur = db.query(func.max(GeneratedDocument.version)).filter(
                GeneratedDocument.artifact_id == artifact_id).scalar()
            return int(cur or 0) + 1
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"doc_skill_worker: version lookup failed for {artifact_id}: {exc}")
        return 2  # safe: it's a revision, so not v1


def build_doc_skill_job(payload: dict) -> None:
    """RQ job: run agent-authored docx-js in the sandbox → store doc + preview.

    payload: { job_id, format, title, code, user_id, chat_id }
    """
    job_id  = payload.get("job_id", "unknown")
    user_id = payload.get("user_id", "unknown")
    chat_id = payload.get("chat_id")

    from core.log_job_context import job_log_context
    with job_log_context(
        job_id=job_id, user_id=user_id, chat_id=chat_id or "",
        request_id=payload.get("request_id") or "",
        agent_id="doc_skill_worker.build_doc_skill_job",
    ):
        return _build_doc_skill_job_impl(payload)


def _build_doc_skill_job_impl(payload: dict) -> None:
    job_id  = payload.get("job_id", "unknown")
    fmt     = (payload.get("format") or "docx").lower()
    title   = (payload.get("title") or "Document").strip()
    code    = payload.get("code") or ""
    images  = payload.get("images") or []
    artifact_id = payload.get("artifact_id") or None
    user_id = payload.get("user_id", "unknown")
    chat_id = payload.get("chat_id")

    if not code.strip():
        _fail(job_id, "No build code provided.")
        return

    _publish_progress(job_id, 1, 4, "Preparing", f"Setting up {fmt.upper()} build…")

    try:
        from sandbox.doc_executor import build, supported_formats
        if fmt not in supported_formats():
            _fail(job_id, f"Unsupported format {fmt!r} (supported: {', '.join(supported_formats())}).")
            return
        if fmt == "pptx" and _PPTX_SHIM_JS not in code:
            code = f"{_PPTX_SHIM_JS}\n\n{code}"
        _publish_progress(job_id, 2, 4, "Building Document", f"Running {fmt.upper()} sandbox…")
        result = build(code, fmt, images=images)
    except Exception as exc:
        logger.error(f"doc_skill_worker: executor crashed for job {job_id}: {exc}")
        _fail(job_id, f"Document sandbox error: {exc}")
        return

    if not result.ok:
        logger.warning(f"doc_skill_worker: build failed job {job_id}: {result.error}")
        # Surface the real build error so the agent can fix its code and retry.
        # Prefix with the format so the user/agent knows which step failed.
        _fmt_label = fmt.upper()
        _fail(job_id, f"{_fmt_label} build failed: {result.error or 'Document build failed'}")
        return

    _publish_progress(job_id, 3, 4, "Saving File", "Writing document to disk…")

    _user_dir = user_doc_dir(user_id, chat_id)
    file_id  = str(_uuid_mod.uuid4())
    ext      = result.ext
    # Versioning: a build is v(N+1) of its artifact, or v1 for a brand-new one.
    artifact = artifact_id or file_id
    version  = _next_artifact_version(artifact)
    filename = f"{_slug(title)}.{ext}"
    path     = os.path.join(_user_dir, f"{file_id}.{ext}")
    try:
        _atomic_write_bytes(path, result.doc_bytes)
    except Exception as exc:
        _fail(job_id, f"File write error: {exc}")
        return

    # Persist page-image preview alongside (best-effort).
    preview_pages = 0
    for i, img in enumerate(result.page_images, start=1):
        try:
            _atomic_write_bytes(
                os.path.join(_user_dir, f"{file_id}.page-{i}.jpg"), img
            )
            preview_pages = i
        except Exception:
            break

    _publish_progress(job_id, 4, 4, "Finalizing", "Recording audit trail…")

    # Audit record (so GET /docs/download/{file_id} can resolve + RBAC-scope it).
    try:
        _save_audit(
            file_id=file_id, job_id=job_id, user_id=user_id, chat_id=chat_id,
            fmt=ext, title=title, filename=filename, file_path=path, content_md=code[:200000],
            artifact_id=artifact, version=version,
        )
    except Exception as exc:
        logger.warning(f"doc_skill_worker: audit save failed (non-fatal): {exc}")

    result_payload = {
        "status":        "done",
        "file_id":       file_id,
        "filename":      filename,
        "format":        ext,
        "size":          len(result.doc_bytes),
        "artifact_id":   artifact,
        "version":       version,
        "preview_pages": preview_pages,
        "preview_url":   f"/ainxt/v1/api/docs/preview/{file_id}" if preview_pages else "",
    }
    _R.setex(f"doc:result:{job_id}", RESULT_TTL, json.dumps(result_payload))
    logger.info(
        f"doc_skill_worker: job {job_id} done — {filename} "
        f"({len(result.doc_bytes)} bytes, {preview_pages} preview pages)"
    )
    # Opportunistic, throttled retention sweep (no-op most calls).
    try:
        purge_expired_docs()
    except Exception:
        pass
