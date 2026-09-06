# SPDX-License-Identifier: MIT
"""Build Studio Knowledge Base upload proxy.

Mirrors ``routers/docs_router.py::upload_doc`` (the sidebar Knowledge Base
endpoint at ``POST /ainxt/v1/api/kb/upload``) but skips the admin approval
queue so documents uploaded from inside a Build Studio workflow become
immediately searchable by ``app/core/kb_retriever.py``.

Why a separate router instead of editing ``routers/docs_router.py``:
the sidebar KB flow (``ai-ui/src/components/KnowledgeBase.jsx``) must keep
its PENDING_APPROVAL / Inbox / maker-checker semantics intact. By exposing
a Build-Studio-only endpoint under ``/ainxt/v1/api/abs/kb/upload-build-studio``
we get auto-approve semantics for this surface alone — the sidebar's
endpoint, request shape, rate limit, and approval workflow are unchanged.

Reuses (does NOT duplicate):
  * ``core.file_validator.validate_upload`` — extension + magic byte +
    size validation.
  * ``core.document_parser.parse_file_structured`` — same parser as the
    sidebar route, so PDF/DOCX/PPTX behaviour stays identical.
  * ``agents.compliance_engine.compliance_engine`` — same PII detection +
    redaction pass that blocks PAN/CVV/AADHAAR.
  * ``store.docs_store.upload_doc`` — the shared persistence + chunking
    + embedding helper. We pass ``auto_approve=True``; the store handles
    the immediate ``activate_doc()`` call that writes to
    ``document_embeddings``.

The body of this handler is intentionally kept structurally identical to
``routers/docs_router.py::upload_doc`` so future changes to compliance,
parsing, or validation in the platform are easy to mirror here.
"""
from __future__ import annotations

import asyncio
import errno
import json as _json
import os as _os
import tempfile
from typing import List, Optional

# Sentinel prefix used on file_path stored in document_embeddings so the
# retriever (``kb_retriever._display_name``) can strip it before showing
# the LLM a ``[doc: …]`` citation. Uniqueness across concurrent uploads
# of files sharing an ``original_filename`` is already guaranteed by
# ``core.file_validator._sanitise_filename``, which prefixes
# ``uuid.hex[:8]_`` per call — this marker is purely for citation
# hygiene. The user-visible ``KnowledgeDocument.name`` is set from
# ``original_filename`` upstream and is unaffected.
from app.core.kb_retriever import ABS_FILENAME_PREFIX as _ABS_FILENAME_PREFIX

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.api.deps import require_access
from app.models import AuthenticatedUser
from app.core import ocr_pipeline
from app.core.ocr_pipeline import ExtractionOptions
from app.core.parser_errors import is_parser_error, PARSER_ERROR_PREFIXES


from core.logger import logger
router = APIRouter(prefix="/kb", tags=["abs-kb"])

# Whitelist + size cap — kept in lockstep with ``routers/docs_router.py``
# so the Build Studio uploader rejects exactly the same files the sidebar
# would reject. Hardcoded (not imported) to avoid coupling this module to
# private constants of the platform router.
#
# Image extensions are now added on top via ocr_pipeline.supported_extensions
# so the Build Studio KB accepts standalone PNG/JPG/TIFF/BMP/WEBP that the
# sidebar still rejects. This is a deliberate scope expansion for the
# Build Studio surface only.
_TEXT_DOC_EXTENSIONS = frozenset({
    "pdf", "docx", "md",
    "ppt", "pptx",
    "html",
    "txt",
    # Spreadsheet formats — core.document_parser handles xlsx/xls/csv.
    "xlsx", "xls", "csv",
})
_ALLOWED_DOC_EXTENSIONS = ocr_pipeline.supported_extensions(_TEXT_DOC_EXTENSIONS)
_KB_MAX_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB per document

# Windows holds an exclusive share-lock on tempfiles for a brief window after
# ``NamedTemporaryFile.__exit__`` returns. With the frontend worker pool firing
# 2-3 parses simultaneously, ``pdf_backend.open`` on one request's tempfile can
# hit ``WinError 32`` from a sibling request's lingering handle. The lock here
# serialises parses inside this process; the embedding step still runs in
# parallel via ``asyncio.to_thread(_upload, …)``. POSIX is unaffected.
_PARSE_LOCK: Optional[asyncio.Lock] = None

# Retry timing tuned for Windows Defender's typical post-write scan window
# (~200 ms on a 25 MB doc). One retry would not be enough; three is overkill.
_PARSE_RETRY_DELAYS_MS = (100, 250)


def _get_parse_lock() -> asyncio.Lock:
    """Lazy-init the parse lock on first use so it binds to the running event
    loop, not whichever loop happened to be active at module import time.
    """
    global _PARSE_LOCK
    if _PARSE_LOCK is None:
        _PARSE_LOCK = asyncio.Lock()
    return _PARSE_LOCK


def _write_tempfile(data: bytes, ext: str) -> str:
    """Write to a tempfile and close the handle before returning the path.

    ``NamedTemporaryFile`` keeps the kernel handle open across its context, and
    on Windows that handle blocks readers — the exact cause of the WinError 32
    this module is working around. ``mkstemp`` gives us a raw FD we close
    explicitly before the parser opens the path.
    """
    fd, path = tempfile.mkstemp(suffix=f".{ext}")
    try:
        _os.write(fd, data)
    finally:
        _os.close(fd)
    return path


def _safe_unlink(path: str) -> None:
    """Best-effort tempfile cleanup. AV scanners on Windows occasionally hold
    a just-closed tempfile briefly; swallow that rather than surfacing a 500.
    The OS sweeps Temp anyway so a leftover .tmp is never a real problem.
    """
    if not path:
        return
    try:
        _os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:  # pragma: no cover — host-specific
        logger.debug(f'[AGENT] abs_kb: tempfile unlink skipped ({path}): {e}')


def _is_share_violation(exc: BaseException) -> bool:
    """``True`` if ``exc`` looks like a transient file-share lock.

    Prefer structured attributes (``winerror`` on Windows, ``errno`` on POSIX)
    over string matching — wrapped exceptions from threadpool tasks preserve
    these attributes through ``run_in_threadpool``.
    """
    winerror = getattr(exc, "winerror", None)
    if winerror == 32:  # ERROR_SHARING_VIOLATION
        return True
    err = getattr(exc, "errno", None)
    if err in (errno.EBUSY, errno.EACCES, errno.ETXTBSY):
        return True
    # The pdf_backend engine wraps the OS error in a RuntimeError without
    # preserving errno, so fall back to a substring check for that failure mode.
    return "being used by another process" in str(exc).lower()


async def _parse_with_retry(tmp_path: str, ext: str, original_filename: str):
    """Run ``parse_file_structured`` under the serialising lock, retrying
    transient share-lock failures from external scanners.
    """
    from core.document_parser import parse_file_structured

    async with _get_parse_lock():
        for attempt, delay_ms in enumerate((0,) + _PARSE_RETRY_DELAYS_MS):
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000.0)
            try:
                return await run_in_threadpool(
                    parse_file_structured, tmp_path, ext, original_filename,
                )
            except Exception as e:  # pragma: no cover — race-only path
                if not _is_share_violation(e) or attempt == len(_PARSE_RETRY_DELAYS_MS):
                    raise
                logger.info(f"[AGENT] abs_kb: parse retry {attempt + 1} for '{original_filename}' after share-lock: {e}")

# Sentinel parser error prefixes have moved to ``app.core.parser_errors``
# so ``app.api.documents`` and ``app.core.ocr_pipeline`` can share them.
# This module-local alias is kept for any external import that targeted the
# old name. Prefer ``is_parser_error()`` for new code.
_PARSER_ERROR_PREFIXES = PARSER_ERROR_PREFIXES


@router.post("/upload-build-studio")
async def upload_build_studio_doc(
    namespace:      str               = Form(...),
    files:          List[UploadFile]  = File(...),
    visibility:     str               = Form("PUBLIC"),
    # JSON array e.g. '["Payments","Finance"]'; [] = org-wide
    department_ids: str               = Form("[]"),
    current_user:   AuthenticatedUser = Depends(require_access),
):
    """Upload one or more documents and auto-approve them for the workflow.

    Behaviour delta vs. ``routers/docs_router.py::upload_doc``:
      1. Iterates ``files`` (multi-file is the primary use case here).
      2. Calls ``store.docs_store.upload_doc(..., auto_approve=True)``,
         which writes vectors to ``document_embeddings`` inside the same
         request (see ``store/docs_store.py`` L341-354).

    NOTE: The platform rate limiter
    (``core.rate_limiter.enforce_rate_limit_with_behaviour``) is intentionally
    omitted. The sidebar route still rate-limits all external KB ingest;
    Build Studio is an authenticated, internal surface invoked from the
    workflow editor and does not need its own limit on top.
    """
    # Lazy imports — avoid pulling the heavy store + parser stacks at
    # gateway boot; the platform router uses the same pattern.
    # ``parse_file_structured`` is imported lazily inside ``_parse_with_retry``
    # so the parser stack only loads on the first actual parse call.
    from store.docs_store import upload_doc as _upload
    from core.file_validator import validate_upload

    # Parse the JSON array of department ids defensively — frontend always
    # sends a JSON string but tolerate any malformed payload by falling
    # back to "no departments" (== PUBLIC behaviour) rather than 400-ing.
    try:
        dept_ids = _json.loads(department_ids)
        if not isinstance(dept_ids, list):
            dept_ids = []
    except Exception:
        dept_ids = []

    uploader_email = current_user.email or ""
    uploader_dept  = current_user.department or ""

    # Visibility hygiene — mirrors the platform router. PUBLIC uploads are
    # org-wide regardless of what the client sent. PRIVATE uploads default
    # to the uploader's own department unless the caller has approver
    # rights (in which case the multi-select wins).
    try:
        from auth.rbac import can_approve as _can_approve  # type: ignore
        _is_approver = _can_approve({
            "email": uploader_email,
            "role":  current_user.role or "",
            "department": uploader_dept,
        })
    except Exception:
        # Fall safe — treat as non-approver if rbac module is unavailable.
        _is_approver = False

    if visibility.upper() == "PUBLIC":
        dept_ids = []
    elif visibility.upper() == "PRIVATE" and not _is_approver:
        dept_ids = [uploader_dept] if uploader_dept else dept_ids

    results: list[dict] = []

    for f in files:
        original_filename = f.filename or "upload"
        data = await f.read()

        # ── Security: extension + magic-byte + size validation ─────────
        vr = validate_upload(
            filename=original_filename,
            content=data,
            allowed_extensions=_ALLOWED_DOC_EXTENSIONS,
            max_size_bytes=_KB_MAX_SIZE_BYTES,
            caller="abs_kb_router",
        )
        if not vr.valid:
            logger.warning(f"[AGENT] abs_kb: upload rejected for '{original_filename}': {vr.error}")
            ext_label = f".{vr.extension}" if vr.extension else "unknown"
            if vr.threat == "html_script_tag":
                error_msg = vr.error
            elif vr.threat == "magic_mismatch":
                error_msg = (
                    f'Upload failed: the content of "{original_filename}" '
                    f"does not match its file type. Please ensure the file "
                    f"is a valid {ext_label.upper()} file."
                )
            else:
                error_msg = (
                    f'Unsupported file type "{ext_label}". Allowed: PDF, DOCX, '
                    f"MD, PPT, PPTX, HTML, TXT, XLSX, XLS, CSV, PNG, JPG, "
                    f"TIFF, BMP, WEBP."
                )
            results.append({
                "success":  False,
                "filename": original_filename,
                "error":    error_msg,
            })
            continue

        # ``vr.safe_filename`` already carries a per-upload ``uuid.hex[:8]_``
        # prefix from ``core.file_validator._sanitise_filename``, so two
        # concurrent uploads of ``notes.pdf`` cannot collide on
        # ``UniqueConstraint("repo","file_path","chunk_index")``. We only
        # add ``_ABS_FILENAME_PREFIX`` so the retriever can strip it from
        # citations shown to the LLM.
        safe_filename = f"{_ABS_FILENAME_PREFIX}{vr.safe_filename}"
        ext = vr.extension

        # ── Step 1: Extract text via the hybrid OCR pipeline ──────────
        # The new ``ocr_pipeline.extract`` orchestrates:
        #   * structured parsing (parse_file_structured),
        #   * multi-engine table extraction (pdfplumber + Camelot lattice + stream),
        #   * embedded-image OCR,
        #   * scanned/error PDF OCR fallback,
        #   * unstructured-data salvage pass,
        #   * optional Gemini Vision for charts,
        #   * content-hash result cache.
        # Image files (PNG/JPG/TIFF/...) are now accepted standalone.
        #
        # The existing Windows share-lock workaround stays in
        # ``_parse_with_retry`` for callers that still need direct parse
        # access, but the pipeline writes its own tempfile and does not
        # need it for the orchestrated path.
        extraction_options = ExtractionOptions(
            force_ocr=False,
            describe_visuals=False,
            ocr_lang="en",
            extract_images=True,
            extract_tables=True,
        )
        try:
            extraction = await run_in_threadpool(
                ocr_pipeline.extract,
                raw_bytes=data,
                ext=ext,
                filename=original_filename,
                options=extraction_options,
            )
            parsed_text = extraction.text
        except Exception as parse_err:
            logger.error(f"[AGENT] abs_kb: pipeline failed for '{original_filename}': {parse_err}")
            results.append({
                "success":  False,
                "filename": original_filename,
                "error":    f"Could not extract text from file: {parse_err}",
            })
            continue

        if extraction.warnings:
            logger.info(f"[AGENT] abs_kb: '{original_filename}' extracted via engine={extraction.engine} with {len(extraction.warnings)} warning(s); first={extraction.warnings[0][:160]}")

        # ── Step 1b: detect parser sentinel error strings ──────────────
        # Belt-and-braces: the pipeline already drops sentinels, but if a
        # future regression let one through we still must not store it as
        # document content (parsers signal failure via bracketed sentinel
        # strings, not exceptions).
        if is_parser_error(parsed_text):
            logger.warning(f"[AGENT] abs_kb: parser sentinel slipped through for '{original_filename}'")
            results.append({
                "success":  False,
                "filename": original_filename,
                "error":    parsed_text.strip("[]"),
            })
            continue

        # ── Step 1c: empty-content guard ───────────────────────────────
        # If parsed_text is still empty after OCR fallback, fail the upload
        # with a clear, actionable error instead of letting docs_store
        # ingest an empty document.
        if not parsed_text.strip():
            if ext == "pdf":
                err_msg = (
                    "PDF appears to be scanned or image-only and OCR could not "
                    "recover readable text. Try a higher-quality scan, or upload "
                    "a born-digital PDF."
                )
            elif ocr_pipeline.is_image_ext(ext):
                err_msg = (
                    "Image contained no readable text and could not be "
                    "described by Vision. Try a higher-resolution image."
                )
            else:
                err_msg = "File parsed but contained no readable text."
            results.append({
                "success":  False,
                "filename": original_filename,
                "error":    err_msg,
            })
            continue

        # ── Step 2: Compliance check + redaction ───────────────────────
        # Mirrors the platform router's compliance pass — we always use
        # redacted_text downstream so a PII-redacted version is what gets
        # chunked + stored, never the raw parsed text. Compliance scanning
        # is a synchronous regex/ML sweep over the full document and can
        # stall the event loop on a 25 MB file, so we offload to a thread.
        redacted_text = parsed_text
        if parsed_text:
            try:
                from agents.compliance_engine import (  # type: ignore
                    compliance_engine, BLOCKING_TYPES,
                )
                check = await run_in_threadpool(
                    compliance_engine.validate_input, parsed_text,
                )
                findings = check.get("findings", [])
                redacted_text = check.get("redacted_text") or parsed_text

                if check.get("was_redacted"):
                    logger.info(f"[AGENT] abs_kb: PII redacted in '{original_filename}' — types={check.get('redacted_types', [])}")

                if check.get("blocked", False):
                    # Use .get() — a finding with no ``type`` key must not
                    # crash the comprehension. (Defensive: BLOCKING_TYPES
                    # membership already returns False for None.)
                    compliance_reasons = sorted({
                        f.get("type") for f in findings
                        if f.get("type") in BLOCKING_TYPES
                    })
                    block_reason = (
                        ", ".join(compliance_reasons)
                        if compliance_reasons else "PCI/PII data"
                    )
                    logger.warning(f"[AGENT] abs_kb: '{original_filename}' BLOCKED by compliance — {compliance_reasons}")
                    results.append({
                        "success":            False,
                        "blocked":            True,
                        "filename":           original_filename,
                        "block_reason":       block_reason,
                        "compliance_reasons": compliance_reasons,
                    })
                    continue
            except Exception as ce:
                # Fail-open: a compliance engine outage must not break
                # uploads. The store still runs its own checks downstream.
                logger.warning(f"[AGENT] abs_kb: compliance check error for '{original_filename}': {ce}")

        # ── Step 3: Store + embed via the shared helper ────────────────
        # auto_approve=True is the entire reason this router exists. The
        # helper writes status=AUTO_APPROVED and invokes activate_doc()
        # inline, populating document_embeddings so kb_retriever sees it
        # on the next workflow run.
        result = await asyncio.to_thread(
            _upload,
            file_bytes=data,
            filename=safe_filename,
            original_filename=original_filename,
            namespace=namespace,
            uploaded_by=uploader_email or None,
            visibility=visibility,
            department_ids=dept_ids,
            department=uploader_dept or None,
            auto_approve=True,
            pre_parsed_text=redacted_text,
        )
        # Plumb OCR pipeline metadata through so the Build Studio progress
        # card can show engine / images / tables / warnings. Non-destructive —
        # docs_store's own keys win on conflict via dict.update order.
        if isinstance(result, dict):
            extraction_meta = {
                "engine":             extraction.engine,
                "page_count":         extraction.page_count,
                "images_extracted":   len(extraction.images),
                "tables_extracted":   len(extraction.tables),
                "warnings":           extraction.warnings[:5],
                "cache_hit":          extraction.cache_hit,
            }
            # Existing docs_store fields take precedence so we never
            # accidentally shadow ``success``/``filename``/``chunk_count``.
            for k, v in extraction_meta.items():
                result.setdefault(k, v)
        results.append(result)

    # Match the platform router's return shape so the frontend handler
    # logic does not have to special-case Build Studio responses.
    if len(results) == 1:
        return results[0]
    return {"results": results}
