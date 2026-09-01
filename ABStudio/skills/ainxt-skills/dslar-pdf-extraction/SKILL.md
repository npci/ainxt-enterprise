---
name: dslar-pdf-extraction
description: Deterministically extract text, tables, image references, and image metadata from AiNxt DL-SAR audit PDFs without passing image bytes in workflow JSON.
---

# DSLAR PDF Extraction

Use this skill whenever a DSLAR/AiNxt DL-SAR audit validation workflow needs to ingest or extract content from a PDF audit report.

## Mandatory workflow rule

For DSLAR AiNxt Audit Validation extraction, do not manually infer PDF structure. Run the bundled deterministic extractor script with `code_executor`, then pass its JSON output to downstream agents.

Bundled script:

```text
scripts/extract_dslar_pdf.py
```

The script returns both:

- `ingested_doc`: page-level PDF ingestion output
- `extracted`: validation-ready normalized content

For workflow handoff, write this payload to `WORKFLOW_ARTIFACT_DIR/extracted.json` and return only a compact artifact/status JSON from the agent.

## Inputs

The extraction agent should pass:

- `pdf_path`: absolute or workspace-relative path to a PDF
- `artifact_dir`: workflow artifact directory; use `WORKFLOW_ARTIFACT_DIR`
- `output_json`: `WORKFLOW_ARTIFACT_DIR/extracted.json`

If both uploaded PDF bytes and a PDF path are available, use the PDF path. The extractor copies/materializes the PDF to `<artifact_dir>/input.pdf` and returns that path as `ingested_doc.source_path` inside `extracted.json`.

## How to run

Use exactly one `code_executor` call with Python code to execute the bundled script absolute path exposed in the skill manifest. Do not use shell/bash command snippets. Do not make exploratory calls, inspect the PDF separately, run the extractor more than once, or call any other tool for this node.

Example code:

```python
import contextlib
import io
import json
import runpy
import sys
from pathlib import Path

script_path = r"<absolute_path_from_skill_manifest>/scripts/extract_dslar_pdf.py"
pdf_path = r"<absolute_or_workspace_relative_pdf_path>"

output_json = str(Path(WORKFLOW_ARTIFACT_DIR) / "extracted.json")

sys.argv = [
    "extract_dslar_pdf.py",
    "--pdf-path", pdf_path,
    "--artifact-dir", WORKFLOW_ARTIFACT_DIR,
    "--output-json", output_json,
]
with contextlib.redirect_stdout(io.StringIO()):
    runpy.run_path(script_path, run_name="__main__")

work_dir = Path(WORKFLOW_ARTIFACT_DIR)
print(json.dumps({
    "artifact_dir": str(work_dir),
    "extracted_json": output_json,
    "source_pdf": str(work_dir / "input.pdf"),
    "status": "extraction_complete",
}, ensure_ascii=False))
```

The script writes the extraction payload to `extracted.json` and also prints JSON to stdout for compatibility. The Document Ingester final response must not echo the full payload; return only compact control JSON.

Strict final-output contract:

- The final response must be one compact JSON object only.
- It must include `artifact_dir`, `extracted_json`, `source_pdf`, and `status`.
- `status` must be `extraction_complete`.
- Do not summarize, explain, or reformat as prose.
- Do not wrap it in Markdown or code fences.
- Never include full extracted content or `base64` in the agent output.

## Output schema

```json
{
  "ingested_doc": {
    "source_path": "ABStudio/backend/runtime_artifacts/workflows/<workflow_run_id>/input.pdf",
    "pages": [
      {
        "page_index": 0,
        "text": "string",
        "tables": [],
        "image_refs": ["page_0_img_12"],
        "image_metadata": [
          {
            "page": 0,
            "ref": "page_0_img_12",
            "xref": 12,
            "ext": "png",
            "mime_type": "image/png",
            "byte_size": 84291,
            "sha256": "..."
          }
        ]
      }
    ]
  },
  "extracted": {
    "full_text": "string",
    "sections": [],
    "tables": [],
    "images": [
      {
        "page": 0,
        "ref": "page_0_img_12",
        "xref": 12,
        "ext": "png",
        "mime_type": "image/png",
        "byte_size": 84291,
        "sha256": "...",
        "description": ""
      }
    ],
    "ingested": {}
  }
}
```

## Behavior guarantees

- Text is extracted via `core.pdf_backend` `page.get_text()`.
- Tables are extracted via `core.pdf_backend` `page.find_tables()` when available.
- Image references are emitted as `page_<page_index>_img_<xref>`.
- Image metadata includes `xref`, `ext`, `mime_type`, `byte_size`, and `sha256`.
- Base64 image payloads are never emitted in workflow JSON.
- OCR is not performed.
- Table and image extraction errors do not stop extraction.
