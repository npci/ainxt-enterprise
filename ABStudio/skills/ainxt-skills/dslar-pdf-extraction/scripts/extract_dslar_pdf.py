# SPDX-License-Identifier: Apache-2.0
"""Deterministic PDF extractor for AiNxt DL-SAR audit validation workflows."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


@contextlib.contextmanager
def _suppress_native_output():
    """Silence C-extension stdout/stderr noise emitted by MuPDF while parsing."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    try:
        with tempfile.TemporaryFile() as sink, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            yield
    finally:
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)


def _str_cell(cell: Any) -> str:
    return "" if cell is None else str(cell)


def _mime_type_for_ext(ext: str) -> str:
    ext = (ext or "png").lower()
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "tif": "image/tiff",
        "tiff": "image/tiff",
        "bmp": "image/bmp",
        "jp2": "image/jp2",
    }.get(ext, f"image/{ext}")


def _extract_tables(page: Any) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    try:
        with _suppress_native_output():
            found_tables = page.find_tables()
        for table in found_tables:
            rows = table.extract() or []
            tables.append([[_str_cell(cell) for cell in row] for row in rows])
    except Exception:
        pass
    return tables


def _materialize_pdf(pdf_path: str, artifact_dir: str | None) -> Path:
    source = Path(pdf_path).expanduser().resolve()
    if not artifact_dir:
        return source

    target_dir = Path(artifact_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "input.pdf"
    if source != target:
        shutil.copy2(source, target)
    return target


def _extract_images(doc: Any, page: Any, page_index: int) -> tuple[list[str], list[dict[str, Any]]]:
    image_refs: list[str] = []
    image_metadata: list[dict[str, Any]] = []

    try:
        images = page.get_images()
    except Exception:
        return image_refs, image_metadata

    for image in images:
        try:
            xref = int(image[0])
            ref = f"page_{page_index}_img_{xref}"
            image_refs.append(ref)
        except Exception:
            continue

        try:
            info = doc.extract_image(xref)
            image_bytes = info.get("image") or b""
            ext = (info.get("ext") or "png").lower()
            image_metadata.append({
                "page": page_index,
                "ref": ref,
                "xref": xref,
                "ext": ext,
                "mime_type": _mime_type_for_ext(ext),
                "byte_size": len(image_bytes),
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
            })
        except Exception:
            image_metadata.append({
                "page": page_index,
                "ref": ref,
                "xref": xref,
                "description": "",
            })

    return image_refs, image_metadata


def extract_pdf(pdf_path: str, artifact_dir: str | None = None) -> dict[str, Any]:
    from core import pdf_backend as fitz

    # Capture the ORIGINAL filename before materialization, because
    # _materialize_pdf copies the input to "input.pdf" inside the artifact dir.
    source_name = Path(pdf_path).expanduser().name

    path = _materialize_pdf(pdf_path, artifact_dir)
    doc = fitz.open(path)
    try:
        pages: list[dict[str, Any]] = []
        for page_index, page in enumerate(doc):
            try:
                text = page.get_text() or ""
            except Exception:
                text = ""

            tables = _extract_tables(page)
            image_refs, image_metadata = _extract_images(doc, page, page_index)

            pages.append({
                "page_index": page_index,
                "text": text,
                "tables": tables,
                "image_refs": image_refs,
                "image_metadata": image_metadata,
            })

        ingested_doc = {
            "source_path": str(path),
            "source_name": source_name,
            "pages": pages,
        }

        full_text_parts: list[str] = []
        sections: list[dict[str, Any]] = []
        flat_tables: list[dict[str, Any]] = []
        normalized_images: list[dict[str, Any]] = []

        for page in pages:
            page_index = page["page_index"]
            text = page.get("text") or ""
            if text:
                full_text_parts.append(text)
                heading = next((line.strip() for line in text.splitlines() if line.strip()), "")[:100]
                sections.append({
                    "page": page_index,
                    "heading": heading,
                    "text": text,
                })

            for table_index, table in enumerate(page.get("tables") or []):
                flat_tables.append({
                    "page": page_index,
                    "table_index": table_index,
                    "rows": table,
                    "context": text[:500],
                })

            metadata = page.get("image_metadata") or []
            if metadata:
                for image in metadata:
                    normalized = dict(image)
                    normalized["description"] = ""
                    normalized_images.append(normalized)
            else:
                for ref in page.get("image_refs") or []:
                    normalized_images.append({
                        "page": page_index,
                        "ref": ref,
                        "description": "",
                    })

        extracted = {
            "source_name": source_name,
            "full_text": "\n\n".join(full_text_parts),
            "sections": sections,
            "tables": flat_tables,
            "images": normalized_images,
            "ingested": ingested_doc,
        }

        return {
            "ingested_doc": ingested_doc,
            "source_name": source_name,
            "extracted": extracted,
        }
    finally:
        doc.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract AiNxt DL-SAR audit PDF content as JSON.")
    parser.add_argument("--pdf-path", required=True, help="Path to the PDF audit report.")
    parser.add_argument("--artifact-dir", default=os.getenv("WORKFLOW_ARTIFACT_DIR", ""), help="Workflow artifact directory used to store input.pdf.")
    parser.add_argument("--output-json", help="Optional path to write the extracted JSON payload.")
    parser.add_argument("--describe-images", default="false", help="Deprecated compatibility flag; image metadata is always extracted without base64.")
    args = parser.parse_args()

    with _suppress_native_output():
        result = extract_pdf(args.pdf_path, artifact_dir=args.artifact_dir or None)
    output = json.dumps(result, ensure_ascii=True)
    if args.output_json:
        output_path = Path(args.output_json).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
