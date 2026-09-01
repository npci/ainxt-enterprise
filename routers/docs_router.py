# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DOCS ROUTER — Document Knowledge Base endpoints
# POST /docs/upload  — upload + embed document(s)
# GET  /docs         — list documents
# GET  /docs/namespaces — list namespaces
# DELETE /docs/{id}  — delete document + embeddings
# ============================================================

import asyncio
import os
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from auth.dependencies import get_current_user
from core.file_validator import validate_upload
from core.logger import logger
from core.rate_limiter import enforce_rate_limit_with_behaviour, DOCS_UPLOAD
from core.security_validation import validate_docs_upload_scope, validate_free_text, _flatten_errors

router = APIRouter(prefix="/kb", tags=["docs"])

# Whitelist: document types accepted for knowledge-base ingestion
_ALLOWED_DOC_EXTENSIONS = frozenset({
    "pdf", "docx", "md",
    "ppt", "pptx",
    "html",
    "txt", "log"
})

_KB_MAX_SIZE_BYTES = 25 * 1024 * 1024   # 25 MB per document
_REPLICA_SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
_REPLICA_SAFE_DOC_ID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


@router.post("/internal/replicate-file")
async def replicate_file_internal(
        doc_id: str = Form(...),
        ext: str = Form(...),
        kind: str = Form("file"),
        source_node: str = Form("unknown"),
        file: UploadFile = File(...),
):
    if not _REPLICA_SAFE_DOC_ID_RE.fullmatch((doc_id or "").strip()):
        raise HTTPException(status_code=400, detail="Invalid doc_id")
    _ext = (ext or "").strip().lower().lstrip(".")
    if not _REPLICA_SAFE_EXT_RE.fullmatch(_ext):
        raise HTTPException(status_code=400, detail="Invalid extension")

    from core.config import KB_DOC_STORAGE_PATH as _KB_FS_ROOT
    data = await file.read()
    os.makedirs(_KB_FS_ROOT, mode=0o755, exist_ok=True)
    abs_path = os.path.join(_KB_FS_ROOT, f"{doc_id}.{_ext}")
    tmp_path = abs_path + ".tmp"
    with open(tmp_path, "wb") as fh:
        fh.write(data)
    os.replace(tmp_path, abs_path)
    logger.info(
        f"[KB_REPLICA][step=write] doc_id={doc_id} ext={_ext} kind={kind} "
        f"path={abs_path} bytes={len(data):,} source_node={source_node}"
    )
    return {"success": True, "doc_id": doc_id, "ext": _ext, "bytes": len(data)}


@router.post("/internal/delete-replica-file")
async def delete_replica_file_internal(payload: dict):
    doc_id = str(payload.get("doc_id") or "").strip()
    ext = str(payload.get("ext") or "").strip().lower().lstrip(".")
    kind = str(payload.get("kind") or "file")
    source_node = str(payload.get("source_node") or "unknown")
    if not _REPLICA_SAFE_DOC_ID_RE.fullmatch(doc_id):
        raise HTTPException(status_code=400, detail="Invalid doc_id")
    if not _REPLICA_SAFE_EXT_RE.fullmatch(ext):
        raise HTTPException(status_code=400, detail="Invalid extension")

    from core.config import KB_DOC_STORAGE_PATH as _KB_FS_ROOT
    abs_path = os.path.join(_KB_FS_ROOT, f"{doc_id}.{ext}")
    deleted = False
    if os.path.isfile(abs_path):
        os.remove(abs_path)
        deleted = True
    logger.info(
        f"[KB_REPLICA][step=delete] doc_id={doc_id} ext={ext} kind={kind} "
        f"path={abs_path} deleted={deleted} source_node={source_node}"
    )
    return {"success": True, "doc_id": doc_id, "ext": ext, "deleted": deleted}


# Known parser error strings returned by document_parser.py when extraction fails.
# Checked after parsing so these are never stored as document content.
# Must stay in sync with return strings in core/document_parser.py.
_PARSER_ERROR_PREFIXES = (
    "[PDF parse error",
    "[PDF parsing unavailable",
    "[DOCX parse error",
    "[DOCX parsing unavailable",
    "[PPTX parse error",
    "[PPTX parsing unavailable",
    "[Presentation has no text content]",   # empty .pptx — no slides have text
    "[Legacy .ppt format",
    "[HTML parse error",
    "[HTML parsing unavailable",
    "[TXT read error",
    "[Excel parse error",
    "[Excel parsing unavailable",
    "[Excel file is empty]",                # valid .xlsx but all sheets are empty
    "[CSV parse error",
    "[CSV parsing unavailable",
    "[RTF parse error",
    "[RTF parsing unavailable",
    "[JSON parse error",
)


@router.post("/upload")
async def upload_doc(
        request:        Request,
        namespace:      str            = Form(...),
        files:          List[UploadFile] = File(...),
        visibility:     str            = Form("PUBLIC"),
        department_ids: str            = Form("[]"),   # JSON array e.g. '["Payments","Finance"]'; [] = org-wide
        # Phase 1 — spec scope metadata
        product_id:     Optional[str]  = Form(None),   # UUID of product (from products table)
        domain:         str            = Form(""),      # e.g. "Tech", "HR", "Finance"
        spec_version:   str            = Form(""),      # e.g. "v3", "2025.1"
        version_date:   str            = Form(""),      # ISO date string e.g. "2025-01-15"
        deprecate_prior: str           = Form("false"), # "true" / "false" — Form sends strings
        parent_doc_id:  Optional[str]  = Form(None),   # prior version doc_id (lineage)
        # ── Part U13 (2026-06-08) — docx §8 hierarchy: doc kind ──
        # BRD / FSD / TPMC_DECISION / RBI_CIRCULAR / ARCHITECTURE / SPEC / OTHER.
        # Empty string from the form is normalised to None in docs_store.upload_doc
        # (the same path that validates against the CHECK enum).
        source_type:    str            = Form(""),
        current_user=Depends(get_current_user),
):
    # ── Rate limit: 20 KB uploads per 5 minutes per user/IP (behaviour-aware) ─
    _uid = current_user.get("sub") or current_user.get("email") if isinstance(current_user, dict) else None
    enforce_rate_limit_with_behaviour(request, DOCS_UPLOAD, user_id=_uid)

    import json as _json
    from store.docs_store import upload_doc as _upload
    from auth.rbac import can_approve as _can_approve

    try:
        dept_ids = _json.loads(department_ids)
        if not isinstance(dept_ids, list):
            dept_ids = []
    except Exception:
        dept_ids = []

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # domain/spec_version are short KB-scope tags, identifier allow-list.
    _dv_ok, _dv_errs, _dv_san = validate_docs_upload_scope(domain, spec_version)
    if not _dv_ok:
        raise HTTPException(status_code=400, detail=_flatten_errors(_dv_errs))
    domain = _dv_san["domain"] or ""
    spec_version = _dv_san["spec_version"] or ""

    _uploader_dept = (
        current_user.get("department", "") if isinstance(current_user, dict)
        else getattr(current_user, "department", "") or ""
    )

    # PUBLIC uploads are org-wide — dept_ids must be [] (enforce regardless of client)
    if visibility.upper() == "PUBLIC":
        dept_ids = []
    # PRIVATE uploads by non-approvers: lock to uploader's own department
    elif visibility.upper() == "PRIVATE" and not _can_approve(current_user):
        dept_ids = [_uploader_dept] if _uploader_dept else dept_ids
    # All uploads go through approval queue → Inbox notification fires for all
    _auto_approve = False

    import time as _time
    results = []
    for f in files:
        _upload_start = _time.perf_counter()
        original_filename = f.filename or "upload"
        data = await f.read()

        # ── [UPLOAD][step=validate] ──────────────────────────────────────────
        vr = validate_upload(
            filename=original_filename,
            content=data,
            allowed_extensions=_ALLOWED_DOC_EXTENSIONS,
            max_size_bytes=_KB_MAX_SIZE_BYTES,
            caller="docs_router",
        )
        if not vr.valid:
            logger.warning(
                f"[UPLOAD][step=validate][ERROR] doc='{original_filename}' "
                f"size={len(data):,}B error='{vr.error}' threat={vr.threat or 'none'}"
            )
            ext_label = f".{vr.extension}" if vr.extension else "unknown"
            if vr.threat == "html_script_tag":
                error_msg = vr.error
            elif vr.threat == "magic_mismatch":
                error_msg = f'Upload failed: the content of "{original_filename}" does not match its file type. Please ensure the file is a valid {ext_label.upper()} file.'
            else:
                error_msg = f'Unsupported file type "{ext_label}". Only PDF, DOCX, MD, PPT, PPTX, HTML, and TXT files are allowed.'
            results.append({"success": False, "filename": original_filename, "error": error_msg})
            continue

        safe_filename = vr.safe_filename
        ext = vr.extension
        logger.info(
            f"[UPLOAD][step=validate] doc='{original_filename}' "
            f"ext={ext} size={len(data):,}B status=ok"
        )

        # ── [UPLOAD][step=docx_to_pdf] ───────────────────────────────────────
        # When the user uploads a DOCX, convert it to PDF using LibreOffice
        # headless before any further processing. The converted PDF becomes the
        # canonical file for the rest of the pipeline (parse, compliance, store,
        # Docling activation). This produces significantly cleaner MD output
        # compared to Docling parsing the raw DOCX directly — Word formatting
        # artefacts (e.g. "Formatted: Highlight" annotations, broken SmartArt
        # character runs) are eliminated in the PDF render.
        #
        # The original DOCX filename is preserved for display; only the stored
        # binary and extension change to PDF.
        #
        # Non-fatal: if LibreOffice is unavailable or conversion fails, the
        # original DOCX bytes are used as-is (existing behaviour).
        if ext == "docx":
            import subprocess as _sp
            import tempfile as _tf
            import os as _os_conv
            _conv_start = _time.perf_counter()
            _converted = False
            try:
                with _tf.TemporaryDirectory() as _conv_dir:
                    # Write DOCX bytes to a temp file
                    _docx_tmp = _os_conv.path.join(_conv_dir, "input.docx")
                    with open(_docx_tmp, "wb") as _fh:
                        _fh.write(data)
                    # Run LibreOffice headless conversion.
                    # The gateway process runs as root on this server and
                    # LibreOffice works correctly as root. No user switching
                    # or environment overrides needed.
                    _proc = _sp.run(
                        [
                            "libreoffice", "--headless",
                            "--convert-to", "pdf",
                            _docx_tmp,
                            "--outdir", _conv_dir,
                        ],
                        timeout=120,
                        capture_output=True,
                    )
                    _pdf_tmp = _os_conv.path.join(_conv_dir, "input.pdf")
                    if _proc.returncode == 0 and _os_conv.path.isfile(_pdf_tmp):
                        with open(_pdf_tmp, "rb") as _pfh:
                            _pdf_bytes = _pfh.read()
                        if len(_pdf_bytes) > 0:
                            # Replace data + ext with the converted PDF
                            data = _pdf_bytes
                            ext  = "pdf"
                            # Keep safe_filename base, change extension to .pdf
                            _base = _os_conv.path.splitext(safe_filename)[0]
                            safe_filename = f"{_base}.pdf"
                            # Also update original_filename extension so that
                            # docs_store derives original_ext="pdf" correctly —
                            # it uses original_filename to set the extension on
                            # the DB row (used for download and Docling activation).
                            _orig_base = _os_conv.path.splitext(original_filename)[0]
                            original_filename = f"{_orig_base}.pdf"
                            _converted = True
                            _conv_ms = (_time.perf_counter() - _conv_start) * 1000
                            logger.info(
                                f"[UPLOAD][step=docx_to_pdf] doc='{original_filename}' "
                                f"pdf_bytes={len(data):,} latency={_conv_ms:.0f}ms status=ok"
                            )
                        else:
                            logger.warning(
                                f"[UPLOAD][step=docx_to_pdf] doc='{original_filename}' "
                                f"LibreOffice produced empty PDF — falling back to DOCX"
                            )
                    else:
                        _stderr = (_proc.stderr or b"").decode(errors="replace")[:300]
                        logger.warning(
                            f"[UPLOAD][step=docx_to_pdf] doc='{original_filename}' "
                            f"LibreOffice exit={_proc.returncode} stderr='{_stderr}' "
                            f"— falling back to DOCX"
                        )
            except FileNotFoundError:
                logger.warning(
                    f"[UPLOAD][step=docx_to_pdf] doc='{original_filename}' "
                    f"LibreOffice not found — falling back to DOCX"
                )
            except Exception as _conv_err:
                _conv_ms = (_time.perf_counter() - _conv_start) * 1000
                logger.warning(
                    f"[UPLOAD][step=docx_to_pdf] doc='{original_filename}' "
                    f"conversion error='{_conv_err}' latency={_conv_ms:.0f}ms "
                    f"— falling back to DOCX"
                )

        # ── [UPLOAD][step=parse] ─────────────────────────────────────────────
        # Docling is intentionally SKIPPED here (skip_docling=True).
        # Reason: if the user uploads and then deletes the doc before approval,
        # a costly Docling/PaddleOCR call would have run for nothing.
        # Docling runs inside activate_doc() AFTER the approver approves.
        # Legacy parsers (markitdown, python-docx, etc.) run here for a quick
        # text extraction needed for compliance redaction and chunking.
        _parse_start = _time.perf_counter()
        try:
            from core.document_parser import parse_file_structured
            import tempfile, os as _os
            tmp_path = None
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                parsed_doc = await run_in_threadpool(
                    parse_file_structured, tmp_path, ext, original_filename,
                    skip_docling=True,   # Docling deferred to activate_doc() post-approval
                )
                parsed_text = parsed_doc.get("content", "") or ""
            finally:
                if tmp_path and _os.path.exists(tmp_path):
                    _os.unlink(tmp_path)
            _parse_ms = (_time.perf_counter() - _parse_start) * 1000
            logger.info(
                f"[UPLOAD][step=parse] doc='{original_filename}' "
                f"parser=legacy chars={len(parsed_text):,} latency={_parse_ms:.0f}ms"
            )
        except Exception as _parse_err:
            _parse_ms = (_time.perf_counter() - _parse_start) * 1000
            logger.error(
                f"[UPLOAD][step=parse][ERROR] doc='{original_filename}' "
                f"latency={_parse_ms:.0f}ms error='{_parse_err}'"
            )
            results.append({
                "success":  False,
                "filename": original_filename,
                "error":    f"Could not extract text from file: {_parse_err}",
            })
            continue

        # ── [UPLOAD][step=parse_check] ───────────────────────────────────────
        # Parsers return bracketed error strings when extraction fails.
        # Catch them here so they are never stored as document content.
        if parsed_text.startswith(_PARSER_ERROR_PREFIXES):
            logger.warning(
                f"[UPLOAD][step=parse_check][ERROR] doc='{original_filename}' "
                f"parser_error='{parsed_text[:120]}'"
            )
            results.append({
                "success":  False,
                "filename": original_filename,
                "error":    parsed_text.strip("[]"),
            })
            continue

        # ── [UPLOAD][step=scan_detect] ───────────────────────────────────────
        # When a PDF yields 0 chars from the legacy parser, check whether it is a
        # scanned (image-only) PDF before rejecting it.  Scanned PDFs have no
        # embedded text — PaddleOCR will extract text during activate_doc() after
        # approval.  Non-PDF zero-char files (or corrupt PDFs that are not scanned)
        # are rejected here with a clear error.
        _is_scanned_pdf = False
        _scan_tmp = None   # temp file path — reused by mixed_scan_detect below
        if not parsed_text.strip() and ext == "pdf":
            try:
                from core.docling_parser import pdf_likely_scanned as _pdf_likely_scanned
                import tempfile as _tmpmod2, os as _os2
                with _tmpmod2.NamedTemporaryFile(delete=False, suffix=".pdf") as _st:
                    _st.write(data)
                    _scan_tmp = _st.name
                try:
                    _is_scanned_pdf = _pdf_likely_scanned(_scan_tmp)
                finally:
                    if not _is_scanned_pdf:
                        # Not scanned — clean up temp file now (mixed_scan_detect won't need it)
                        if _scan_tmp and _os2.path.exists(_scan_tmp):
                            _os2.unlink(_scan_tmp)
                        _scan_tmp = None
                if _is_scanned_pdf:
                    logger.info(
                        f"[UPLOAD][step=scan_detect] doc='{original_filename}' "
                        f"scanned_pdf=true — OCR+compliance deferred to activate_doc()"
                    )
                else:
                    logger.warning(
                        f"[UPLOAD][step=scan_detect] doc='{original_filename}' "
                        f"scanned_pdf=false chars=0 — rejecting (no extractable text)"
                    )
                    results.append({
                        "success":  False,
                        "filename": original_filename,
                        "error":    "No text could be extracted from this file.",
                    })
                    continue
            except Exception as _sde:
                logger.warning(
                    f"[UPLOAD][step=scan_detect][WARN] doc='{original_filename}' "
                    f"error='{_sde}' — treating as non-scanned, rejecting"
                )
                if _scan_tmp:
                    try:
                        import os as _os2
                        _os2.unlink(_scan_tmp)
                    except Exception:
                        pass
                    _scan_tmp = None
                results.append({
                    "success":  False,
                    "filename": original_filename,
                    "error":    "No text could be extracted from this file.",
                })
                continue

        # ── [UPLOAD][step=mixed_scan_detect] ─────────────────────────────────
        # For PDFs that have SOME extractable text (not fully scanned), check
        # whether any individual pages are image-only. A 60-page PDF where pages
        # 17, 22, 27, 45 are scanned images will pass the scan_detect gate above
        # (first 3 pages have text) but those image pages would be silently lost
        # without this check. We flag them so PaddleOCR runs on those pages at
        # activation time and the results are merged with the digital pages' text.
        _has_mixed_scanned_pages = False
        if ext == "pdf" and not _is_scanned_pdf and parsed_text.strip():
            try:
                from core.docling_parser import pdf_has_any_scanned_pages as _pdf_has_mixed
                import tempfile as _tmpmod3, os as _os3
                _mixed_tmp = None
                with _tmpmod3.NamedTemporaryFile(delete=False, suffix=".pdf") as _mt:
                    _mt.write(data)
                    _mixed_tmp = _mt.name
                try:
                    _has_mixed_scanned_pages = _pdf_has_mixed(_mixed_tmp)
                finally:
                    if _mixed_tmp and _os3.path.exists(_mixed_tmp):
                        _os3.unlink(_mixed_tmp)
                if _has_mixed_scanned_pages:
                    logger.info(
                        f"[UPLOAD][step=mixed_scan_detect] doc='{original_filename}' "
                        f"has_mixed_scanned_pages=true — PaddleOCR will run on scanned "
                        f"pages at activation time"
                    )
            except Exception as _mse:
                logger.warning(
                    f"[UPLOAD][step=mixed_scan_detect][WARN] doc='{original_filename}' "
                    f"error='{_mse}' — mixed scan detection skipped (non-fatal)"
                )

        # Clean up the scan_detect temp file if it's still around (fully-scanned path)
        if _scan_tmp:
            try:
                import os as _os_cleanup
                if _os_cleanup.path.exists(_scan_tmp):
                    _os_cleanup.unlink(_scan_tmp)
            except Exception:
                pass
            _scan_tmp = None

        # ── [UPLOAD][step=compliance] ────────────────────────────────────────
        # Gated by COMPLIANCE_SCAN_KB_UPLOAD env flag (default OFF).
        # When OFF: raw parsed text is stored as-is; redaction happens at
        # retrieval time for cloud models only (gateway.py _bypass_safety_filters).
        # When ON: PII/PCI scan + redaction runs here; blocking types reject the upload.
        from core.config import COMPLIANCE_SCAN_KB_UPLOAD as _KB_COMPLIANCE_ON
        _comp_start   = _time.perf_counter()
        redacted_text = parsed_text   # default: raw text, no compliance
        if _KB_COMPLIANCE_ON and parsed_text and not _is_scanned_pdf:
            try:
                from agents.compliance_engine import compliance_engine, BLOCKING_TYPES
                check    = compliance_engine.validate_input(parsed_text)
                findings = check.get("findings", [])
                redacted_text = check.get("redacted_text") or parsed_text
                _comp_ms = (_time.perf_counter() - _comp_start) * 1000
                if check.get("was_redacted"):
                    logger.info(
                        f"[UPLOAD][step=compliance] doc='{original_filename}' "
                        f"redacted=true types={check.get('redacted_types', [])} "
                        f"latency={_comp_ms:.0f}ms"
                    )
                else:
                    logger.info(
                        f"[UPLOAD][step=compliance] doc='{original_filename}' "
                        f"redacted=false latency={_comp_ms:.0f}ms"
                    )
                if check.get("blocked", False):
                    compliance_reasons = sorted(set(
                        f["type"] for f in findings
                        if f.get("type") in BLOCKING_TYPES
                    ))
                    block_reason = ", ".join(compliance_reasons) if compliance_reasons else "PCI/PII data"
                    logger.warning(
                        f"[UPLOAD][step=compliance][ERROR] doc='{original_filename}' "
                        f"blocked=true reasons={compliance_reasons}"
                    )
                    results.append({
                        "success":            False,
                        "blocked":            True,
                        "filename":           original_filename,
                        "block_reason":       block_reason,
                        "compliance_reasons": compliance_reasons,
                    })
                    continue
            except Exception as _ce:
                _comp_ms = (_time.perf_counter() - _comp_start) * 1000
                logger.warning(
                    f"[UPLOAD][step=compliance][WARN] doc='{original_filename}' "
                    f"latency={_comp_ms:.0f}ms error='{_ce}' — proceeding with unredacted text"
                )
        else:
            logger.info(
                f"[UPLOAD][step=compliance] doc='{original_filename}' "
                f"skipped — COMPLIANCE_SCAN_KB_UPLOAD=false"
            )

        # ── [UPLOAD][step=store] ─────────────────────────────────────────────
        # Pass pre_parsed_text=redacted_text so docs_store skips its internal
        # re-parse and uses the legacy content for chunking.
        # Docling will re-parse (and re-chunk) inside activate_doc() on approval.
        # For scanned PDFs, pre_parsed_text="" and is_scanned_pdf=True — the store
        # bypasses the empty-text rejection and defers OCR to approval.
        # For mixed PDFs, has_mixed_scanned_pages=True flags that PaddleOCR will
        # run on the scanned pages at activation time.
        _store_start = _time.perf_counter()
        logger.info(
            f"[UPLOAD][step=store] doc='{original_filename}' "
            f"namespace={namespace} chars={len(redacted_text):,}"
            + (" scanned_pdf=true" if _is_scanned_pdf else "")
            + (" mixed_scanned=true" if _has_mixed_scanned_pages else "")
        )
        result = await asyncio.to_thread(
            _upload,
            file_bytes=data,
            filename=safe_filename,
            original_filename=original_filename,
            namespace=namespace,
            uploaded_by=current_user.get("email") if isinstance(current_user, dict) else getattr(current_user, "email", None),
            visibility=visibility,
            department_ids=dept_ids,
            department=_uploader_dept or None,
            auto_approve=_auto_approve,
            pre_parsed_text=redacted_text,
            product_id=product_id or None,
            domain=domain or None,
            spec_version=spec_version or None,
            version_date=version_date or None,
            deprecate_prior=(deprecate_prior.lower() == "true"),
            parent_doc_id=parent_doc_id or None,
            source_type=source_type or None,
            is_scanned_pdf=_is_scanned_pdf,
            has_mixed_scanned_pages=_has_mixed_scanned_pages,
        )
        _store_ms    = (_time.perf_counter() - _store_start) * 1000
        _elapsed_ms  = (_time.perf_counter() - _upload_start) * 1000

        if result.get("success"):
            logger.info(
                f"[UPLOAD][step=complete] doc='{original_filename}' "
                f"doc_id={result.get('doc_id')} chunks={result.get('chunk_count')} "
                f"status={result.get('status')} store_ms={_store_ms:.0f}ms "
                f"total_ms={_elapsed_ms:.0f}ms"
            )
        else:
            logger.error(
                f"[UPLOAD][step=store][ERROR] doc='{original_filename}' "
                f"store_ms={_store_ms:.0f}ms error='{result.get('error')}'"
            )
        results.append(result)

    if len(results) == 1:
        return results[0]
    return {"results": results}


@router.get("")
async def list_docs(
        namespace:    Optional[str] = Query(None),
        status:       Optional[str] = Query(None),   # filter by status e.g. PENDING_APPROVAL
        product_id:   Optional[str] = Query(None),   # filter by spec scope: product
        domain:       Optional[str] = Query(None),   # filter by spec scope: domain
        spec_version: Optional[str] = Query(None),   # filter by spec scope: version
        limit:        int           = Query(50),
        offset:       int           = Query(0),
        current_user=Depends(get_current_user),
):
    from store.docs_store import list_docs as _list
    from auth.rbac import is_admin, can_approve as _can_approve

    _is_admin    = is_admin(current_user)
    _can_approve_ = _can_approve(current_user)
    _user_email  = current_user.get("email", "") if isinstance(current_user, dict) else getattr(current_user, "email", "") or ""
    all_docs     = _list(
        namespace=namespace,
        status=status,
        product_id=product_id,
        domain=domain,
        spec_version=spec_version,
    )

    if _is_admin:
        docs = all_docs
    else:
        user_dept = current_user.get("department", "") if isinstance(current_user, dict) else ""
        docs = []
        for d in all_docs:
            doc_status = d.get("status", "PENDING_APPROVAL")
            # PENDING_APPROVAL: visible to approvers (inbox) OR to the uploader themselves
            is_own_pending = False
            if doc_status == "PENDING_APPROVAL" and not _can_approve_:
                if d.get("uploaded_by") != _user_email:
                    continue
                is_own_pending = True   # confirmed: this is the user's own pending doc
            # Dept filter — skip for uploader's own pending docs: their dept may differ
            # from what was stored (org_tree sync timing), and they should always see
            # docs they themselves uploaded.
            if not is_own_pending:
                dept_ids = d.get("department_ids") or []
                if dept_ids and user_dept not in dept_ids:
                    continue
            # Dept filter for pending docs: scope Approval Inbox to same dept as uploader
            if doc_status == "PENDING_APPROVAL" and _can_approve_:
                uploader_dept = d.get("uploaded_by_dept") or ""
                if uploader_dept and user_dept and uploader_dept != user_dept:
                    continue
            docs.append(d)
    return {
        "docs":  docs[offset : offset + limit],
        "total": len(docs),
    }


@router.get("/namespaces")
async def list_namespaces(current_user=Depends(get_current_user)):
    from store.docs_store import list_namespaces as _list_ns

    return {"namespaces": _list_ns()}


# ============================================================
# KB DELETION HISTORY  (2026-08-06)
#
# GET /kb/deleted-history — paginated, ACL-filtered list of ACTIVE knowledge_docs
# that were subsequently hard-deleted (see store/docs_store.py::delete_doc()).
# Docs that were deleted while PENDING_APPROVAL/REJECTED never appear here —
# they never went live and no snapshot was written for them.
#
# ACL rule matrix:
#   ┌─────────────────────┬────────────┬──────────────────┬────────────────────┐
#   │ Scenario             │ Uploader   │ Own team admin   │ Other team admin   │
#   ├─────────────────────┼────────────┼──────────────────┼────────────────────┤
#   │ PRIVATE doc deleted  │ Yes        │ Yes              │ No                 │
#   │ PUBLIC doc deleted   │ Yes        │ Yes              │ Yes                │
#   └─────────────────────┴────────────┴──────────────────┴────────────────────┘
#   "super-admin" (role == "admin")            -> sees every row, unfiltered.
#   "team-admin" (HOD, is_hod/get_hod_departments) -> sees PUBLIC deletions
#       org-wide, plus PRIVATE deletions where the uploader's OR deleter's
#       department (or the doc's department_ids scope) is one of their own
#       HOD department(s). Never sees another team's PRIVATE deletions.
#   "regular user" -> sees only rows where they were the uploader or the one
#       who performed the deletion, regardless of visibility.
# ============================================================

def _kb_deletion_visible(row: dict, current_user: dict) -> bool:
    """Non-raising ACL check for a single knowledge_doc_deletions row.
    See the rule matrix in the docstring above this function's call site."""
    from auth.rbac import is_admin, is_hod, get_hod_departments

    if is_admin(current_user):
        return True

    _email = current_user.get("email", "") if isinstance(current_user, dict) else getattr(current_user, "email", "") or ""
    if _email and (row.get("uploaded_by") == _email or row.get("deleted_by") == _email):
        return True

    if is_hod(current_user):
        if (row.get("visibility") or "PUBLIC").upper() == "PUBLIC":
            return True
        hod_depts = set(get_hod_departments(current_user))
        if hod_depts:
            if row.get("uploaded_by_dept") in hod_depts or row.get("deleted_by_dept") in hod_depts:
                return True
            _dept_ids = row.get("department_ids") or []
            if hod_depts.intersection(_dept_ids):
                return True

    return False


@router.get("/deleted-history")
async def list_deleted_history(
        page:      int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=200),
        current_user=Depends(get_current_user),
):
    """Paginated, ACL-filtered KB deletion audit trail. See module docstring
    above _kb_deletion_visible() for the full rule matrix."""
    from store.docs_store import list_deletion_history as _list_history

    all_rows = _list_history()
    visible = [r for r in all_rows if _kb_deletion_visible(r, current_user)]

    total = len(visible)
    start = (page - 1) * page_size
    items = visible[start : start + page_size]

    return {
        "items":     items,
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


@router.get("/{doc_id}")
async def get_doc(
        doc_id: str,
        current_user=Depends(get_current_user),
):
    from store.docs_store import list_docs as _list
    all_docs = _list()
    doc = next((d for d in all_docs if d.get("id") == doc_id), None)
    if not doc:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.post("/{doc_id}/approve")
async def approve_doc(
        doc_id: str,
        current_user=Depends(get_current_user),
):
    from auth.rbac import can_approve as _can_approve, is_admin as _is_admin, is_request_approver as _is_request_approver
    from db.database import SessionLocal
    from db.models import KnowledgeDocument
    from datetime import datetime, timezone

    approver_email = current_user.get("email") if isinstance(current_user, dict) else getattr(current_user, "email", None)

    # ── 1. Update status in knowledge_docs ────────────────────────────────
    db = SessionLocal()
    _doc_name = doc_id  # captured before db.close() for use in enqueue payload
    try:
        doc = db.get(KnowledgeDocument, doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        # Authorisation: admin, OR the uploader's own HOD / one of the HOD's
        # nominated delegatees, OR (fallback, when the uploader has no
        # resolvable HOD) any ad_level<=3 senior approver — mirrors the
        # admin-broadcast fallback in _notify_approvers_kb.
        if not (_is_admin(current_user)
                or _is_request_approver(current_user, doc.uploaded_by or "")
                or _can_approve(current_user)):
            raise HTTPException(status_code=403, detail="Only the uploader's HOD (or their delegate) or an admin can approve documents.")

        # Guard: already in progress or completed — return current state
        if doc.status in ("APPROVED", "INDEXING", "ACTIVE"):
            logger.info(
                f"[APPROVE] doc_id={doc_id} already in status={doc.status} — skipping re-enqueue"
            )
            return {"success": True, "status": doc.status, "message": "Already approved"}

        # ── Maker-Checker: uploader cannot approve their own document ─────
        if doc.uploaded_by and approver_email and doc.uploaded_by == approver_email:
            raise HTTPException(
                status_code=403,
                detail="Maker-checker violation: the user who uploaded this document cannot approve it. A different user must perform the approval."
            )

        # Capture doc name before session closes
        _doc_name       = doc.name or doc_id

        # Set INDEXING — signals to the UI that background parsing has started.
        # The kb_worker will advance this to ACTIVE on success, or roll back to
        # PENDING_APPROVAL on failure so the approver can retry.
        doc.status      = "INDEXING"
        doc.approved_by = approver_email
        doc.approved_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            f"[APPROVE][step=status_set] doc_id={doc_id} "
            f"status=INDEXING approved_by={approver_email}"
        )
    finally:
        db.close()

    # ── 2. Enqueue activate_doc to kb_queue (RQ background worker) ────────────
    # The kb_worker process picks this up and runs Docling parse → chunk →
    # embed → pgvector INSERT with no RQ hard cap; stage-level HTTP timeouts still apply.
    # On success the worker sets status=ACTIVE and notifies the uploader via Inbox.
    # On failure the worker rolls status back to PENDING_APPROVAL for retry.
    _approver_email_bg = approver_email or "approver"

    import uuid as _uuid
    from core.job_queue import enqueue_job, Q_KB
    _job_id = str(_uuid.uuid4())

    logger.info(
        f"[APPROVE][step=enqueue] doc_id={doc_id} "
        f"job_id={_job_id} queue={Q_KB} doc_name='{_doc_name}'"
    )
    try:
        enqueue_job(
            fn_name        = "workers.kb_worker.run_activate_doc",
            payload        = {
                "doc_id":      doc_id,
                "approved_by": _approver_email_bg,
                "doc_name":    _doc_name,
            },
            queue_name     = Q_KB,
            timeout        = None,    # no RQ hard cap; parse/embed stage timeouts still apply
            retry_count    = 1,
            retry_interval = [120],   # retry once after 2 min on transient failure
            job_id         = _job_id,
        )
        logger.info(
            f"[APPROVE][step=enqueue][OK] doc_id={doc_id} "
            f"job_id={_job_id} status=INDEXING"
        )
    except Exception as _eq:
        # Enqueue failed (Redis unavailable or queue at capacity).
        # Roll back to PENDING_APPROVAL so the approver can retry.
        logger.error(
            f"[APPROVE][step=enqueue][ERROR] doc_id={doc_id} "
            f"job_id={_job_id} error='{_eq}' — rolling back to PENDING_APPROVAL"
        )
        _db_rb = SessionLocal()
        try:
            _doc_rb = _db_rb.get(KnowledgeDocument, doc_id)
            if _doc_rb:
                _doc_rb.status = "PENDING_APPROVAL"
                _db_rb.commit()
                logger.warning(
                    f"[APPROVE][step=enqueue][ROLLBACK] doc_id={doc_id} "
                    f"status rolled back to PENDING_APPROVAL"
                )
        finally:
            _db_rb.close()
        raise HTTPException(
            status_code=503,
            detail="Indexing queue is temporarily unavailable. Please retry in a moment."
        )

    return {"success": True, "status": "INDEXING", "message": "Document approved — parsing in progress"}


@router.post("/{doc_id}/reject")
async def reject_doc(
        doc_id: str,
        reason: str = "",
        current_user=Depends(get_current_user),
):
    from auth.rbac import can_approve as _can_approve, is_admin as _is_admin, is_request_approver as _is_request_approver
    from db.database import SessionLocal
    from db.models import KnowledgeDocument
    db = SessionLocal()
    try:
        doc = db.get(KnowledgeDocument, doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        # Authorisation: admin, OR the uploader's own HOD / one of the HOD's
        # nominated delegatees, OR (fallback) any ad_level<=3 senior approver.
        if not (_is_admin(current_user)
                or _is_request_approver(current_user, doc.uploaded_by or "")
                or _can_approve(current_user)):
            raise HTTPException(status_code=403, detail="Only the uploader's HOD (or their delegate) or an admin can reject documents.")

        # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
        # reason is free text, later rendered into the uploader's inbox notice.
        if reason and reason.strip():
            _ok, _errs, reason = validate_free_text(reason)
            if not _ok:
                raise HTTPException(status_code=400, detail=_flatten_errors({"reason": _errs}))

        doc.status           = "REJECTED"
        doc.chunks           = None   # discard staged chunks — doc will never be indexed
        doc.rejection_reason = reason or None
        _doc_name      = doc.name or doc_id
        _uploader_email = doc.uploaded_by or ""
        _doc_visibility = (doc.visibility or "PUBLIC").upper()
        db.commit()

        # Notify submitter via Inbox
        if _uploader_email:
            try:
                from store.inbox_store import publish_inbox_item as _pub_rej
                from db.database import SessionLocal as _SLrej
                from db.models import User as _Urej
                from datetime import datetime as _dtrej, timezone as _tzrej, timedelta as _tdrej
                _ist_rej = (_dtrej.now(_tzrej.utc) + _tdrej(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p IST")
                _rejecter = current_user.get("email") if isinstance(current_user, dict) else getattr(current_user, "email", None)
                _dbrej = _SLrej()
                try:
                    _urej = _dbrej.query(_Urej).filter(_Urej.email == _uploader_email).first()
                    if _urej:
                        _rej_body = f"Your document **{_doc_name}** was **rejected** by `{_rejecter}` on {_ist_rej}."
                        if reason:
                            _rej_body += f"\n\n**Reason:** {reason}"
                        _pub_rej(
                            user_id=str(_urej.id),
                            type="kb_approval",
                            title=f"[KB Rejected] {_doc_name}",
                            body=_rej_body,
                            source_id=doc_id,
                            metadata={"entity_id": doc_id, "status": "REJECTED", "rejected_by": _rejecter,
                                      "uploaded_by": _uploader_email, "visibility": _doc_visibility},
                        )
                finally:
                    _dbrej.close()
            except Exception:
                pass

        return {"success": True, "status": "REJECTED"}
    finally:
        db.close()


@router.delete("/{doc_id}")
async def delete_doc(
        doc_id: str,
        current_user=Depends(get_current_user),
):
    from store.docs_store import delete_doc as _delete
    from db.database import SessionLocal
    from db.models import KnowledgeDocument, InboxItem

    # Fetch the document to check ownership and status before deleting
    was_pending = False
    db = SessionLocal()
    try:
        doc = db.get(KnowledgeDocument, doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        # For pending docs: only the uploader (Maker) may delete
        if doc.status == "PENDING_APPROVAL":
            if current_user.get("email") != doc.uploaded_by:
                raise HTTPException(
                    status_code=403,
                    detail="Only the uploader can retract a pending document",
                )
            was_pending = True
    finally:
        db.close()

    _deleter_email = current_user.get("email", "") if isinstance(current_user, dict) else getattr(current_user, "email", "") or ""
    _deleter_dept  = current_user.get("department", "") if isinstance(current_user, dict) else getattr(current_user, "department", "") or ""
    result = _delete(doc_id, deleted_by=_deleter_email or None, deleted_by_dept=_deleter_dept or None)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Not found"))

    # Clean up checker inbox notifications so they don't linger after retraction.
    # Only kb_approval rows with this source_id exist at this point — approve/reject
    # result notifications are written only after those actions, which cannot have
    # happened while the doc was still PENDING_APPROVAL.
    if was_pending:
        db2 = SessionLocal()
        try:
            db2.query(InboxItem).filter(
                InboxItem.source_id == doc_id,
                InboxItem.type == "kb_approval",
            ).delete(synchronize_session=False)
            db2.commit()
        except Exception as _e:
            logger.warning(f"delete_doc: failed to clean up inbox items for doc {doc_id}: {_e}")
        finally:
            db2.close()

    return result


# ============================================================
# Part U13 (2026-06-08) — original-file download for citation footer
# ============================================================
#
# Source: AiNxt_Retrieval_Discussion_Summary.docx §2 / §13 — retain originals
# alongside the canonical .md so users can verify complex-layout docs.
#
# GET /kb/original/<doc_id>
#   Streams the binary original (PDF/DOCX/XLSX/PPTX) that was retained at
#   upload time. The ext lives on knowledge_docs.original_ext (Part U13).
#   ACL: mirrors the existing GET /kb/<doc_id> path — current_user must be
#   able to read the doc's namespace. We reuse the same list_docs guard.
@router.get("/original/{doc_id}")
async def get_doc_original(
        doc_id: str,
        current_user=Depends(get_current_user),
):
    """Stream the retained original binary for a KB doc."""
    from fastapi.responses import FileResponse
    from store.docs_store import get_original_path, list_docs as _list

    # ── ACL via the existing list_docs read path ──────────────────────────
    # list_docs already applies the namespace + visibility + band filter so
    # any user who can see the doc in /kb/{doc_id} can fetch its original.
    _all = _list()
    _doc_meta = next((d for d in _all if d.get("id") == doc_id), None)
    if not _doc_meta:
        # Hide existence from unauthorised callers — same as get_doc above.
        raise HTTPException(status_code=404, detail="Document not found")

    _original_ext = (_doc_meta.get("original_ext") or "").strip().lower() or None
    _abs = get_original_path(doc_id, original_ext=_original_ext)
    if not _abs:
        # Either the doc had no original retained (legacy upload before U13)
        # or the file is missing on disk (operator concern). Return 404 with
        # a hint so the UI can hide the "Open original" link.
        raise HTTPException(
            status_code=404,
            detail="Original file not retained for this document",
        )

    # MIME-type by extension. application/octet-stream is the safe default —
    # browsers won't render unknown types inline (good — we want users to
    # save the original to verify, not view a rendered version).
    _ext_to_mime = {
        "pdf":  "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ppt":  "application/vnd.ms-powerpoint",
        "doc":  "application/msword",
        "xls":  "application/vnd.ms-excel",
        "html": "text/html; charset=utf-8",
        "txt":  "text/plain; charset=utf-8",
        "md":   "text/markdown; charset=utf-8",
    }
    _mime = _ext_to_mime.get(_original_ext or "", "application/octet-stream")
    # Suggest the original filename to the browser save dialog when possible.
    _suggested = (_doc_meta.get("filename") or _doc_meta.get("name") or f"{doc_id}")
    return FileResponse(
        path=_abs,
        media_type=_mime,
        filename=_suggested,
    )