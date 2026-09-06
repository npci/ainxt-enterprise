# SPDX-License-Identifier: MIT
"""Agent runner attachment endpoints."""
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.concurrency import run_in_threadpool
from app.models import AuthenticatedUser
from app.api.deps import require_access
from app.core import ocr_pipeline
from app.core.ocr_pipeline import ExtractionOptions, IMAGE_EXTENSIONS
from core.file_validator import validate_upload

from core.logger import logger
router = APIRouter()

_AGENT_RUNNER_ATTACHMENT_MAX_CHARS = 60_000
_AGENT_RUNNER_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
# Structured text formats (delegated to core.document_parser).
_AGENT_RUNNER_TEXT_EXTENSIONS = frozenset({
    "pdf", "docx", "pptx", "xlsx", "xls", "xlsm", "csv",
    "html", "htm", "rtf", "txt", "json", "md",
})
# Total accept list — text formats + image formats (handled by ocr_pipeline).
_AGENT_RUNNER_ATTACHMENT_ALLOWED_EXTENSIONS = ocr_pipeline.supported_extensions(
    _AGENT_RUNNER_TEXT_EXTENSIONS,
)
# Supported OCR languages surfaced via the capabilities endpoint. RapidOCR
# ships with English by default; other languages are listed for UI surface
# only — actual language switching happens upstream in core.pdf_ocr.
_AGENT_RUNNER_OCR_LANGUAGES = ("en", "hi", "mr", "ta", "auto")

_XLSX_EXTENSIONS = {"xlsx", "xls", "xlsm"}


def _run_xlsx_pipeline(raw_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Excel upload processing is not supported.

    Always raises a clean 415 so the route returns an unambiguous
    "unsupported format" error to the client.
    """
    raise HTTPException(
        status_code=415,
        detail="Excel (.xlsx/.xls) upload processing is not supported.",
    )


def _format_stat_line(stats: Dict[str, Any]) -> str:
    """One-line stat suffix used in the human-readable summary."""
    parts: List[str] = []
    if "sum" in stats:
        parts.append(f"sum={stats['sum']}")
    if "mean" in stats:
        # Round means to 2 dp so the summary stays readable; the full
        # report JSON below carries the exact value.
        try:
            parts.append(f"mean={round(float(stats['mean']), 2)}")
        except (TypeError, ValueError):
            parts.append(f"mean={stats['mean']}")
    if "min" in stats and "max" in stats:
        parts.append(f"min={stats['min']}  max={stats['max']}")
    if "unique_count" in stats and "non_null_count" in stats:
        parts.append(f"unique={stats['unique_count']}/{stats['non_null_count']}")
    return ("  " + "  ".join(parts)) if parts else ""


def _render_xlsx_report(report: Dict[str, Any], filename: str) -> str:
    """Turn the pipeline JSON into a model-friendly text block.

    A human summary (per-sheet columns, stats, validation) is followed by
    the full data as CSV per sheet so the agent can both reason about the
    shape of the workbook and quote precise figures from every row without
    hallucinating.
    """
    structure = report.get("structure", {})
    validation = report.get("validation", {})
    analysis = report.get("analysis", {})

    sheet_names = structure.get("sheet_names", [])
    total_rows = sum(
        int(structure.get("sheets", {}).get(n, {}).get("row_count", 0))
        for n in sheet_names
    )

    lines: List[str] = []
    lines.append(
        f"Excel workbook \"{filename}\" — "
        f"{len(sheet_names)} sheet{'s' if len(sheet_names) != 1 else ''}, "
        f"{total_rows} total rows."
    )
    overall = validation.get("overall_status", "unknown")
    lines.append(f"Overall validation: {overall}")
    lines.append("")

    for sheet_name in sheet_names:
        s_struct = structure.get("sheets", {}).get(sheet_name, {})
        s_valid = validation.get("sheets", {}).get(sheet_name, {})
        s_ana = analysis.get("sheets", {}).get(sheet_name, {})

        row_count = s_struct.get("row_count", 0)
        col_count = s_struct.get("column_count", 0)
        lines.append(f"## Sheet: {sheet_name} ({row_count} rows × {col_count} columns)")

        cols_meta = s_ana.get("columns") or s_struct.get("columns") or []
        if cols_meta:
            lines.append("Columns:")
            for col in cols_meta:
                name = col.get("name", "?")
                dtype = col.get("dtype") or col.get("pandas_dtype") or ""
                purpose = col.get("purpose")
                stats = col.get("stats") or {}
                suffix = _format_stat_line(stats)
                purpose_str = f", purpose={purpose}" if purpose else ""
                lines.append(f"  - {name}  ({dtype}{purpose_str}){suffix}")

        issues = s_valid.get("issues") or []
        dup_count = (s_valid.get("duplicate_rows") or {}).get("count", 0)
        empty_cols = s_valid.get("empty_columns") or []
        if issues or dup_count or empty_cols:
            lines.append("Validation issues:")
            if dup_count:
                lines.append(f"  - duplicate_rows: {dup_count}")
            for col_name in empty_cols:
                lines.append(f"  - empty_column: {col_name}")
            for issue in issues:
                col = issue.get("column", "?")
                kind = issue.get("issue", "?")
                count = issue.get("count")
                count_str = f" (count={count})" if count is not None else ""
                lines.append(f"  - {kind} in '{col}'{count_str}")
        else:
            lines.append("Validation issues: none.")

        integrity = s_ana.get("integrity_check") or {}
        if integrity:
            passed = integrity.get("passed", True)
            lines.append(
                f"Integrity check: {'passed' if passed else 'FAILED — drift detected'}"
            )
        lines.append("")

    # Full data — every row of every sheet as CSV so the model can quote
    # precise figures from the whole workbook, not just the head/tail preview.
    # The pipeline caps rows on clean boundaries within a char budget, so a
    # truncated sheet arrives whole-row (never cut mid-record).
    full_data = report.get("full_data", {})
    fd_sheets = full_data.get("sheets", {})
    if fd_sheets:
        lines.append("Full data (CSV per sheet):")
        for sheet_name in sheet_names:
            sheet_data = fd_sheets.get(sheet_name, {})
            csv_text = sheet_data.get("csv")
            if not csv_text:
                continue
            total_rows = sheet_data.get("total_rows", 0)
            included_rows = sheet_data.get("included_rows", total_rows)
            if sheet_data.get("truncated"):
                header = (
                    f"### Data - {sheet_name} "
                    f"(showing {included_rows} of {total_rows} rows - "
                    f"ask the user to filter for the rest)"
                )
            else:
                header = f"### Data - {sheet_name} ({total_rows} rows)"
            lines.append(header)
            # Legend maps Excel column letters to names so the model can
            # resolve coordinate questions ("cell I14") without counting.
            legend = sheet_data.get("column_legend")
            if legend:
                lines.append(legend)
            lines.append("```csv")
            lines.append(csv_text.rstrip("\n"))
            lines.append("```")
    return "\n".join(lines)


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _extract_scanned_pdf_text(path: str, filename: str) -> str:
    """Hybrid scanned-PDF fallback.

    Strategy:
      1. Try the local hybrid extractor (``core.pdf_ocr``) — rapidocr-onnxruntime
         based, no external API, no Gemini key required. This is the primary
         path and the one that succeeds for the AiNxt circular use case.
      2. If pdf_ocr is unavailable OR returned empty, fall back to the legacy
         Gemini Vision per-page path. Kept so previously-working deployments
         that rely on GOOGLE_API_KEY do not regress.
    """
    # Path 1 — local hybrid OCR (preferred).
    try:
        from core.pdf_ocr import extract_pdf
        result = extract_pdf(path, filename)
        text = (result.get("text") or "").strip()
        if text:
            return text
    except Exception:
        # Defensive — pdf_ocr is designed not to raise, but a partial install
        # could fail at import time. Fall through to the Gemini path below.
        pass

    # Path 2 — Gemini Vision per page (legacy fallback).
    try:
        from core import pdf_backend as fitz
        from core.document_parser import parse_image
    except ImportError:
        return ""

    doc = fitz.open(path)
    parts: List[str] = []
    try:
        for page_index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            img_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            img_path = img_tmp.name
            img_tmp.close()
            try:
                pix.save(img_path)
                page_text = parse_image(img_path, f"{filename} page {page_index}")
            finally:
                try:
                    os.unlink(img_path)
                except OSError:
                    pass
            if "vision unavailable" in (page_text or "").lower():
                return ""
            if page_text and page_text.strip():
                parts.append(f"## Page {page_index}\n{page_text.strip()}")
    finally:
        doc.close()
    return "\n\n".join(parts)


def _extract_via_pipeline(
    filename: str, raw_bytes: bytes, content_type: str, options: ExtractionOptions,
) -> Dict[str, Any]:
    """Single dispatch point — Excel uploads are unsupported and rejected
    here; everything else flows through ``app.core.ocr_pipeline``.

    Returns the dict that the route returns to the client (the
    ``ocr_pipeline.ExtractionResult.to_response_dict`` envelope, plus
    ``filename``).
    """
    ext = _extension(filename)

    # Excel uploads are not supported — reject with a clean 415.
    if ext in _XLSX_EXTENSIONS or "spreadsheetml" in content_type or "ms-excel" in content_type:
        report = _run_xlsx_pipeline(raw_bytes, filename)
        text = _render_xlsx_report(report, filename)
        full_len = len(text)
        truncated = full_len > _AGENT_RUNNER_ATTACHMENT_MAX_CHARS
        if truncated:
            text = text[:_AGENT_RUNNER_ATTACHMENT_MAX_CHARS]
        return {
            "filename": filename,
            "text": text,
            "char_count": len(text),
            "original_char_count": full_len,
            "truncated": truncated,
            "engine": "xlsx-pipeline",
            "page_count": 0,
            "warnings": [],
            "images_extracted": 0,
            "tables_extracted": 0,
            "cache_hit": False,
        }

    result = ocr_pipeline.extract(
        raw_bytes=raw_bytes,
        ext=ext,
        filename=filename,
        options=options,
        max_chars=_AGENT_RUNNER_ATTACHMENT_MAX_CHARS,
    )
    if not result.text.strip():
        # Surface a clean 400 — the pipeline already attached warnings.
        detail = (
            "File parsed but contained no readable text."
            if not result.warnings
            else "Could not extract text: " + "; ".join(result.warnings[:3])
        )
        raise HTTPException(status_code=400, detail=detail)
    envelope = result.to_response_dict()
    envelope["filename"] = filename
    return envelope



@router.get("/agent-runner/capabilities")
async def agent_runner_capabilities(
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Return what the OCR/extraction pipeline can do.

    The Build Studio UI uses this to enable/disable the Vision toggle,
    keep its accept-list in sync with the backend, and populate the
    language picker. Cheap, no-auth-cost endpoint.

    Also reports which optional OCR libs are *actually importable* in
    the running process so the Settings drawer can surface "pdfplumber
    not installed; install via pip install pdfplumber" instead of
    leaving the user guessing why warnings keep appearing.  This is the
    same import probe the pipeline runs at extract-time, lifted here so
    it can be hit without a file upload.
    """
    import sys as _sys

    def _probe(mod_name: str) -> bool:
        try:
            __import__(mod_name)
            return True
        except Exception:
            return False

    optional_libs = {
        "pdfplumber":           _probe("pdfplumber"),
        "camelot":              _probe("camelot"),
        "pypdfium2":            _probe("pypdfium2"),
        "pillow":               _probe("PIL"),
        "rapidocr_onnxruntime": _probe("rapidocr_onnxruntime"),
        "rapidocr":             _probe("rapidocr"),
    }
    return {
        "vision_available": ocr_pipeline.vision_available(),
        "ocr_engines": ["rapidocr"] + (["gemini-vision"] if ocr_pipeline.vision_available() else []),
        "supported_extensions": sorted(_AGENT_RUNNER_ATTACHMENT_ALLOWED_EXTENSIONS),
        "supported_languages": list(_AGENT_RUNNER_OCR_LANGUAGES),
        "max_size_bytes": _AGENT_RUNNER_ATTACHMENT_MAX_BYTES,
        "max_chars": _AGENT_RUNNER_ATTACHMENT_MAX_CHARS,
        # Diagnostic — which Python is running uvicorn, plus per-lib
        # importability.  If any required lib is False here the chip
        # will surface a "<lib> not installed" warning every upload
        # until the backend is restarted in a Python that can see it.
        "runtime": {
            "python_executable": _sys.executable,
            "python_version": _sys.version.split()[0],
            "optional_libs": optional_libs,
        },
    }


async def _read_bounded(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in bounded chunks, rejecting once ``max_bytes`` is
    exceeded so an oversized file is refused mid-stream instead of being
    fully buffered before the size check.

    A client can lie about (or omit) Content-Length, so we cannot trust the
    header alone — we enforce the cap on the bytes actually streamed in.
    """
    chunk_size = 1024 * 1024  # 1 MB
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {max_bytes // (1024 * 1024)} MB limit.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/agent-runner/attachment")
async def agent_runner_attachment(
    file: UploadFile = File(...),
    force_ocr: Optional[str] = Form(None),
    describe_visuals: Optional[str] = Form(None),
    ocr_lang: Optional[str] = Form(None),
    current_user: AuthenticatedUser = Depends(require_access),
):
    filename = file.filename or "attachment"
    options = ExtractionOptions(
        force_ocr=_parse_bool(force_ocr),
        describe_visuals=_parse_bool(describe_visuals),
        ocr_lang=(ocr_lang or "en").lower(),
    )
    try:
        raw_bytes = await _read_bounded(file, _AGENT_RUNNER_ATTACHMENT_MAX_BYTES)
        validation = validate_upload(
            filename=filename,
            content=raw_bytes,
            allowed_extensions=_AGENT_RUNNER_ATTACHMENT_ALLOWED_EXTENSIONS,
            max_size_bytes=_AGENT_RUNNER_ATTACHMENT_MAX_BYTES,
            caller="agent_runner_attachment",
        )
        if not validation.valid:
            raise HTTPException(status_code=400, detail=validation.error)
        envelope = await run_in_threadpool(
            _extract_via_pipeline, filename, raw_bytes, file.content_type or "", options,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"[AGENT] agent_runner_attachment: extraction failed for '{filename}'")
        raise HTTPException(status_code=500, detail=f"Failed to extract text: {exc}")
    return envelope


def _parse_bool(v: Optional[str]) -> bool:
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ───────────────────────────────────────────────────────────────────────────
# Image-as-asset upload (logos, reference images, figures)
# ───────────────────────────────────────────────────────────────────────────
# Unlike /agent-runner/attachment, which extracts text, this endpoint saves
# the uploaded image to GENERATED_FILES_DIR so agents can reference it by
# path when generating documents or images (e.g., doc.add_picture("logo.png")).

_AGENT_RUNNER_IMAGE_ASSET_MAX_BYTES = 25 * 1024 * 1024
_AGENT_RUNNER_IMAGE_ASSET_ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS


def _save_image_asset(raw_bytes: bytes, filename: str, user_id: str = "") -> Dict[str, Any]:
    """Persist an uploaded image to GENERATED_FILES_DIR and return asset metadata.

    The file is named ``<uuid>_<original>`` so repeated uploads of the same
    logo never collide, while the original filename is preserved for the
    agent prompt and download header.

    Broken Access Control / IDOR fix: when ``user_id`` is provided the asset is
    stored under the caller's per-user owner-dir
    (``GENERATED_FILES_DIR/{owner_tag}/{name}``) so the download endpoint only
    serves it back to that user. ``disk_name`` / ``download_url`` carry the
    ``{owner_tag}/`` prefix accordingly, and ``asset_path`` points at the real
    on-disk location so the sandbox can still read the image. With no
    ``user_id`` the file stays flat (legacy behaviour).
    """
    # Strip any directory component before we build the unique name or run
    # extension checks. validate_upload also sanitises, but this keeps the
    # storage path construction locally safe regardless of caller.
    filename = Path(filename).name
    ext = _extension(filename)
    if not ext:
        raise HTTPException(status_code=400, detail="Image filename has no extension")
    if ext not in _AGENT_RUNNER_IMAGE_ASSET_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type '{ext}'. Allowed: {', '.join(sorted(_AGENT_RUNNER_IMAGE_ASSET_ALLOWED_EXTENSIONS))}",
        )

    generated_files_dir = os.environ.get("GENERATED_FILES_DIR", "")
    if not generated_files_dir:
        # Align with main.py's fallback so uploaded assets land in the same
        # directory that download_generated_file serves from. A random temp
        # dir would make the file immediately unreachable.
        generated_files_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "tmp"
        )

    base = Path(generated_files_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)

    unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"

    # Per-user isolation: nest the asset under the caller's owner-dir so the
    # download endpoint scopes it to this user. ``disk_name`` is the relative
    # key the URL uses (``{owner_tag}/{name}`` or just ``{name}`` when there is
    # no identity).
    # Imported from the stdlib-only ``app.owner_tag`` rather than ``app.main``:
    # the latter constructs a FastAPI app object and runs
    # ``load_dotenv(override=True)`` at import time, which is far too much to
    # pull in for a 3-line helper.
    try:
        from app.owner_tag import owner_tag as _owner_tag
        tag = _owner_tag(user_id)
    except Exception:
        tag = ""

    if tag:
        write_dir = base / tag
        write_dir.mkdir(parents=True, exist_ok=True)
        disk_name = f"{tag}/{unique_name}"
    else:
        write_dir = base
        disk_name = unique_name

    dest = write_dir / unique_name
    try:
        dest.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")

    with open(dest, "wb") as fh:
        fh.write(raw_bytes)

    return {
        "filename": filename,
        "disk_name": disk_name,
        "asset_path": str(dest),
        "sandbox_name": unique_name,
        "download_url": f"/generated-files/{quote(disk_name, safe='/')}",
        "format": ext,
        "size_bytes": len(raw_bytes),
    }


@router.post("/agent-runner/image-asset")
async def agent_runner_image_asset(
    file: UploadFile = File(...),
    describe_visuals: Optional[str] = Form(None),
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Upload an image as a sandbox asset the agent can reference by path.

    The image is saved to GENERATED_FILES_DIR. The response includes the
    sandbox path and a download URL. If ``describe_visuals=true`` and a
    Gemini key is configured, a short vision description is also returned
    in ``text`` so the agent understands what the image contains.
    """
    filename = file.filename or "image.png"
    try:
        raw_bytes = await _read_bounded(file, _AGENT_RUNNER_IMAGE_ASSET_MAX_BYTES)
        validation = validate_upload(
            filename=filename,
            content=raw_bytes,
            allowed_extensions=_AGENT_RUNNER_IMAGE_ASSET_ALLOWED_EXTENSIONS,
            max_size_bytes=_AGENT_RUNNER_IMAGE_ASSET_MAX_BYTES,
            caller="agent_runner_image_asset",
        )
        if not validation.valid:
            raise HTTPException(status_code=400, detail=validation.error)

        asset = await run_in_threadpool(_save_image_asset, raw_bytes, filename, current_user.id)

        # Optional vision description so the agent knows what the logo/image
        # contains even if it cannot see pixels directly.
        text = ""
        if _parse_bool(describe_visuals) and ocr_pipeline.vision_available():
            try:
                result = await run_in_threadpool(
                    ocr_pipeline.extract,
                    raw_bytes=raw_bytes,
                    ext=asset["format"],
                    filename=filename,
                    options=ExtractionOptions(describe_visuals=True),
                    max_chars=_AGENT_RUNNER_ATTACHMENT_MAX_CHARS,
                )
                text = result.text or ""
            except Exception as exc:
                logger.warning(f"[AGENT] image-asset vision description failed for '{filename}': {exc}")

        return {
            **asset,
            "text": text,
            "char_count": len(text),
            "engine": "vision" if text else "image-asset",
            "warnings": [],
            "images_extracted": 0,
            "tables_extracted": 0,
            "page_count": 0,
            "cache_hit": False,
            "truncated": False,
            "original_char_count": len(text),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"[AGENT] agent_runner_image_asset: failed for '{filename}'")
        raise HTTPException(status_code=500, detail=f"Failed to save image asset: {exc}")
