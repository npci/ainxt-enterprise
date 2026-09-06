# SPDX-License-Identifier: MIT
"""
Host-side document preview rendering.

Generates page-image previews ({file_id}.page-N.jpg) for a generated document so
the in-app Canvas / inline preview can SHOW the document without a download.

The native doc-generation path (workers.doc_worker._skill_generate) writes the
deliverable file but does NOT render previews — only the sandbox PPTX path did.
This module fills that gap for docx/xlsx/pptx/pdf/csv/txt/md.

Rendering strategy (best-effort, first available wins):
  • pdf                         → rasterize directly via core.pdf_backend, no binary
  • docx/xlsx/pptx/csv/txt/md   → convert to PDF, then rasterize via core.pdf_backend
        1. host `soffice`/`libreoffice` on PATH  (primary on the Ubuntu host)
        2. else run soffice inside the doc-sandbox Docker image
        3. else give up (return 0) — caller falls back to text/markdown preview

Design rules:
  • NEVER raises — previews are non-critical. Returns the number of pages written
    (0 on any failure).
  • Reuses the sandbox conventions: {file_id}.page-N.jpg, PREVIEW_DPI, page cap.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from core.logger import logger

# Reuse sandbox constants so previews look identical across paths.
try:
    from sandbox.doc_executor import (
        PREVIEW_DPI as _SANDBOX_DPI,
        MAX_PREVIEW_PAGES as _SANDBOX_MAX_PAGES,
        docker_available as _docker_available,
        image_present as _image_present,
        IMAGE as _SANDBOX_IMAGE,
    )
    _DEFAULT_DPI = int(_SANDBOX_DPI)
    _DEFAULT_MAX_PAGES = int(_SANDBOX_MAX_PAGES)
except Exception:  # pragma: no cover — sandbox module import guard
    _DEFAULT_DPI = int(os.getenv("AINXT_DOC_PREVIEW_DPI", "150"))
    _DEFAULT_MAX_PAGES = int(os.getenv("AINXT_DOC_PREVIEW_PAGES", "20"))
    _SANDBOX_IMAGE = os.getenv("AINXT_DOC_SANDBOX_IMAGE", "ainxt-doc-sandbox:latest")

    def _docker_available() -> bool:  # type: ignore
        return False

    def _image_present() -> bool:  # type: ignore
        return False


# Formats we already hold as a PDF (rasterize directly).
_PDF_NATIVE = {"pdf"}
# Formats LibreOffice can convert to PDF for preview.
_SOFFICE_CONVERTIBLE = {"docx", "doc", "pptx", "ppt", "xlsx", "xls", "csv", "txt", "md", "odt", "rtf"}

_SOFFICE_TIMEOUT_S = int(os.getenv("AINXT_DOC_PREVIEW_SOFFICE_TIMEOUT_S", "120"))


def _soffice_bin() -> str | None:
    """Return the host LibreOffice binary if installed, else None."""
    return shutil.which("soffice") or shutil.which("libreoffice")


def _convert_to_pdf_host(src_path: str, out_dir: str) -> str | None:
    """Convert `src_path` → PDF in `out_dir` using host soffice. Returns pdf path
    or None. Best-effort."""
    soffice = _soffice_bin()
    if not soffice:
        return None
    profile = os.path.join(out_dir, ".loprofile")
    try:
        subprocess.run(
            [
                soffice, "--headless",
                f"-env:UserInstallation=file://{profile}",
                "--convert-to", "pdf", "--outdir", out_dir, src_path,
            ],
            capture_output=True, timeout=_SOFFICE_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning(f"doc_preview: host soffice convert failed: {exc}")
        return None
    pdf_path = os.path.join(out_dir, os.path.splitext(os.path.basename(src_path))[0] + ".pdf")
    return pdf_path if os.path.exists(pdf_path) else None


def _convert_to_pdf_sandbox(src_path: str, out_dir: str) -> str | None:
    """Convert `src_path` → PDF using the doc-sandbox Docker image (which bundles
    LibreOffice). Returns pdf path or None. Best-effort."""
    if not (_docker_available() and _image_present()):
        return None
    workdir = tempfile.mkdtemp(prefix="ainxt-preview-", dir=out_dir)
    try:
        base = os.path.basename(src_path)
        dst = os.path.join(workdir, base)
        shutil.copyfile(src_path, dst)
        try:
            os.chmod(workdir, 0o777)
            os.chmod(dst, 0o644)
        except Exception:
            pass
        run_script = (
            "set -e; cd /work; export HOME=/tmp; "
            "soffice --headless -env:UserInstallation=file:///tmp/loprofile "
            f"--convert-to pdf --outdir /work '{base}' >/dev/null 2>&1 || true"
        )
        cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", "1g", "--cpus", "1",
            "--pids-limit", "256",
            "--read-only", "--tmpfs", "/tmp",
            "-v", f"{workdir}:/work",
            _SANDBOX_IMAGE, "sh", "-c", run_script,
        ]
        subprocess.run(cmd, capture_output=True, timeout=_SOFFICE_TIMEOUT_S, text=True)
        pdf_path = os.path.join(workdir, os.path.splitext(base)[0] + ".pdf")
        if os.path.exists(pdf_path):
            # Move it up to out_dir so the workdir can be cleaned.
            final = os.path.join(out_dir, os.path.splitext(base)[0] + ".preview.pdf")
            shutil.move(pdf_path, final)
            return final
        return None
    except Exception as exc:
        logger.warning(f"doc_preview: sandbox soffice convert failed: {exc}")
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _rasterize_pdf(pdf_path: str, out_dir: str, file_id: str,
                   max_pages: int, dpi: int) -> int:
    """Rasterize a PDF → {file_id}.page-N.jpg via core.pdf_backend. Returns page
    count written (0 on failure)."""
    try:
        from core import pdf_backend as fitz
    except Exception as exc:
        logger.warning(f"doc_preview: pdf_backend unavailable — no preview: {exc}")
        return 0

    zoom = dpi / 72.0  # 72 dpi is the PDF base
    written = 0
    try:
        doc = fitz.open(pdf_path)
        try:
            for i, page in enumerate(doc, start=1):
                if i > max_pages:
                    break
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                out = os.path.join(out_dir, f"{file_id}.page-{i}.jpg")
                # writes JPEG when the extension is .jpg
                pix.save(out)
                written = i
        finally:
            doc.close()
    except Exception as exc:
        logger.warning(f"doc_preview: rasterize failed for {pdf_path}: {exc}")
    return written


def render_preview_pages(
    src_path: str,
    fmt: str,
    out_dir: str,
    file_id: str,
    *,
    max_pages: int | None = None,
    dpi: int | None = None,
) -> int:
    """Render page-image previews for a generated document.

    Writes {out_dir}/{file_id}.page-N.jpg and returns the number of pages.
    Best-effort — returns 0 on any problem and never raises so it cannot fail
    the doc job.

    Args:
        src_path : path to the built deliverable file.
        fmt      : lowercase extension without dot (docx|xlsx|pptx|pdf|csv|txt|md).
        out_dir  : directory to write page images into (the user's DOC dir).
        file_id  : GeneratedDocument id — the page-image filename stem.
    """
    max_pages = int(max_pages or _DEFAULT_MAX_PAGES)
    dpi = int(dpi or _DEFAULT_DPI)
    fmt = (fmt or "").lower().lstrip(".")

    if not src_path or not os.path.exists(src_path):
        return 0

    # 1) Already a PDF → rasterize directly.
    if fmt in _PDF_NATIVE:
        return _rasterize_pdf(src_path, out_dir, file_id, max_pages, dpi)

    # 2) Needs conversion to PDF first.
    if fmt not in _SOFFICE_CONVERTIBLE:
        return 0

    tmp_pdf_dir = tempfile.mkdtemp(prefix="ainxt-preview-pdf-", dir=out_dir)
    try:
        pdf_path = _convert_to_pdf_host(src_path, tmp_pdf_dir)
        if not pdf_path:
            pdf_path = _convert_to_pdf_sandbox(src_path, tmp_pdf_dir)
        if not pdf_path or not os.path.exists(pdf_path):
            logger.info(
                f"doc_preview: no PDF renderer available for '{fmt}' "
                f"(soffice + sandbox both unavailable) — skipping preview"
            )
            return 0
        return _rasterize_pdf(pdf_path, out_dir, file_id, max_pages, dpi)
    except Exception as exc:
        logger.warning(f"doc_preview: render_preview_pages failed (non-fatal): {exc}")
        return 0
    finally:
        shutil.rmtree(tmp_pdf_dir, ignore_errors=True)
