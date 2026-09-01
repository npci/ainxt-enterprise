---
name: dslar-image-enrichment
description: Deterministically enrich DSLAR extracted image records by re-extracting images from the stored parent PDF using xref metadata.
---

# DSLAR Image Enrichment

Use this skill whenever the DSLAR AiNxt Audit Validation workflow needs to convert extracted PDF image references into image descriptions for downstream metadata and clause validation.

## Mandatory workflow rule

Do not manually describe images in the agent prompt. Run the bundled deterministic enrichment script with `code_executor`, write `WORKFLOW_ARTIFACT_DIR/enriched.json`, then return only compact artifact/status JSON.

Bundled scripts:

```text
scripts/materialize_extracted_json.py
scripts/enrich_dslar_images.py
```

The workflow should already have `WORKFLOW_ARTIFACT_DIR/extracted.json`. Run `enrich_dslar_images.py` on that file and write the updated payload to `WORKFLOW_ARTIFACT_DIR/enriched.json`. The enrichment script reads `ingested_doc.source_path` or `extracted.ingested.source_path`, re-opens the parent PDF, extracts each image by `xref`, base64-encodes it only in memory for the Gemini image proxy call, and writes the result into that image's `description` field.

## Inputs

The Image Enricher agent should use `WORKFLOW_ARTIFACT_DIR/extracted.json` written by the previous agents.

Each enrichable image should have:

```json
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
```

The payload must also include a source PDF path at either:

```json
{
  "ingested_doc": {"source_path": ".../input.pdf"}
}
```

or:

```json
{
  "extracted": {"ingested": {"source_path": ".../input.pdf"}}
}
```

## How to run

Use `code_executor` with Python code to execute the bundled scripts exposed in the skill manifest. Do not use shell/bash command snippets.

The previous agents write `WORKFLOW_ARTIFACT_DIR/extracted.json`. Read that file directly, run image enrichment into `WORKFLOW_ARTIFACT_DIR/enriched.json`, and return only compact artifact/status JSON.

Use this Python code pattern inside `code_executor`:

```python
import json
import runpy
import sys
from pathlib import Path

enricher_path = r"<absolute_path_from_skill_manifest>/scripts/enrich_dslar_images.py"

work_dir = Path(WORKFLOW_ARTIFACT_DIR)
extracted_path = work_dir / "extracted.json"
enriched_path = work_dir / "enriched.json"

sys.argv = [
    "enrich_dslar_images.py",
    "--input-json", str(extracted_path),
    "--output-json", str(enriched_path),
    "--describe-images", "true",
    "--provider", "gemini",
    "--model", "gemini-3.5-flash",
]
runpy.run_path(enricher_path, run_name="__main__")

enriched_payload = json.loads(enriched_path.read_text(encoding="utf-8"))
extracted = enriched_payload.get("extracted") if isinstance(enriched_payload, dict) else {}
images = extracted.get("images") if isinstance(extracted, dict) else None
if not isinstance(images, list) and isinstance(enriched_payload, dict):
    images = enriched_payload.get("images")
if not isinstance(images, list):
    images = []
image_count = len(images)
described_image_count = sum(1 for image in images if isinstance(image, dict) and image.get("description"))
failed_image_count = sum(1 for image in images if isinstance(image, dict) and image.get("description_status") == "failed")
empty_response_image_count = sum(1 for image in images if isinstance(image, dict) and image.get("description_status") == "empty_response")

print(json.dumps({
    "artifact_dir": str(work_dir),
    "extracted_json": str(extracted_path),
    "enriched_json": str(enriched_path),
    "image_count": image_count,
    "described_image_count": described_image_count,
    "failed_image_count": failed_image_count,
    "empty_response_image_count": empty_response_image_count,
    "status": "image_enrichment_complete",
}, ensure_ascii=False))
```

If image description is not requested, use `"--describe-images", "false"`; the script will write the JSON to `enriched.json` with any accidental `base64` fields removed.

`materialize_extracted_json.py` remains available for compatibility with raw prior-agent output, but the DSLAR workflow should not need it in the artifact-based path.

The enrichment script writes the updated JSON to `enriched.json`. The Image Enricher final response must not emit the file contents; return compact control JSON containing `artifact_dir`, `extracted_json`, `enriched_json`, `image_count`, `described_image_count`, `failed_image_count`, `empty_response_image_count`, and `status="image_enrichment_complete"`.

## Configuration

For Gemini, the script uses the platform LLM proxy image endpoint when `LLM_PROXY_URL` is available. In local/dev, when `LLM_PROXY_URL` is absent, it uses the OpenAI-compatible `/chat/completions` URL and sends the image as an `image_url` data URL; the platform gateway handles image parts before normal text routing. It reads:

- `LLM_PROXY_URL` for `/llm/generate-image` when available
- `LLM_PROXY_TOKEN` when the proxy requires the internal token header
- `VISION_OPENAI_COMPATIBLE_BASE_URL`, then `OPENAI_COMPATIBLE_BASE_URL`, then `LOCAL_LLM_BASE_URL`, defaulting to `http://localhost:11434/v1` when `LLM_PROXY_URL` is absent
- `VISION_OPENAI_COMPATIBLE_API_KEY`, then `OPENAI_COMPATIBLE_API_KEY`, then `LOCAL_LLM_API_KEY`, defaulting to `not-needed` when `LLM_PROXY_URL` is absent
- `LOCAL_LLM_MODEL`, defaulting to `gemini-3.5-flash`, only when `--model` is not supplied. In local/dev, set this to an accessible vision-capable local model if not using Gemini.

For `--provider openai-compatible`, the script uses only the OpenAI-compatible endpoint path and reads:

- `OPENAI_COMPATIBLE_BASE_URL`, then `LOCAL_LLM_BASE_URL`, defaulting to `http://localhost:11434/v1`
- `OPENAI_COMPATIBLE_API_KEY`, then `LOCAL_LLM_API_KEY`, defaulting to `not-needed`

Use a vision-capable model for `--model`.

## Behavior guarantees

- If `describe_images` is false, returns the input JSON with any accidental `base64` fields removed.
- If there are no images, returns the input JSON.
- Images without `xref` are preserved unchanged except accidental `base64` removal.
- For each image with `xref`, the script re-opens the parent PDF and extracts image bytes by xref.
- Base64 is created only in memory for the multimodal API request and is never returned.
- For Gemini, the script sends `image_b64` and `mime_type` to `/llm/generate-image`; the proxy converts it to Gemini native `inline_data`.
- For `openai-compatible`, the script sends the image as an actual multimodal `image_url` message part, not as plain text.
- Raw model text becomes `image.description` and sets `image.description_status="success"` only when it is not a known invalid/no-image response.
- Empty model responses set `image.description_status="empty_response"` and `image.description_error`.
- Existing `page`, `ref`, `xref`, `mime_type`, `byte_size`, and `sha256` fields are preserved.
- Per-image failures do not fail the whole workflow; the failed image keeps its existing description or an empty string and records `image.description_status="failed"` plus `image.description_error`.
- SSE/streaming chat completion responses are parsed from `data:` chunks and joined from `choices[].delta.content` into `image.description`.
- Non-JSON/non-SSE or HTTP error responses from the vision endpoint also record `image.description_response_preview` with the first 500 characters of the raw response body.
- The JSON structure returned by the previous agent is preserved.
