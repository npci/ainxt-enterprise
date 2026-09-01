# SPDX-License-Identifier: Apache-2.0
"""Deterministic image enrichment for AiNxt DL-SAR audit validation workflows."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import sys
from copy import deepcopy
from typing import Any
from urllib import error, request


DEFAULT_WORKERS = 6
MAX_WORKERS = 8


PROMPT = (
    "Describe this image from an audit report in one or two sentences. "
    "Focus on content relevant to compliance evidence, signatures, seals, "
    "stamps, tables, diagrams, screenshots, architecture, data flow, or audit proof. "
    "If the image is unreadable or not useful for audit validation, say that briefly."
)


class VisionResponseError(RuntimeError):
    def __init__(self, message: str, response_preview: str = "") -> None:
        super().__init__(message)
        self.response_preview = response_preview


def _parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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


def _base_url() -> str:
    return (
        os.getenv("VISION_OPENAI_COMPATIBLE_BASE_URL")
        or os.getenv("OPENAI_COMPATIBLE_BASE_URL")
        or os.getenv("LOCAL_LLM_BASE_URL")
        or "http://localhost:11434/v1"
    ).rstrip("/")


def _api_key() -> str:
    return (
        os.getenv("VISION_OPENAI_COMPATIBLE_API_KEY")
        or os.getenv("OPENAI_COMPATIBLE_API_KEY")
        or os.getenv("LOCAL_LLM_API_KEY")
        or "not-needed"
    )


def _internal_token() -> str:
    """Return the platform gateway's internal token, mirroring the backend's
    ``llm_handler.OpenAIClient`` auth contract.

    The deployed ainxt gateway authenticates OpenAI-compatible agent calls
    with the ``X-Internal-Token`` header, not (only) ``Authorization: Bearer``.
    The working chat/agent runtime sends this header (see
    ``app/core/llm_handler.py`` OpenAIClient ``default_headers``). Without it
    the gateway accepts the request but the vision turn returns
    "Error generating response", which this script records as
    ``vision_model_did_not_process_image``.

    Resolution order matches how the backend resolves the proxy token and the
    compatible API key, so the same .env that powers the chat path powers this
    script too.
    """
    return (
        os.getenv("LLM_PROXY_TOKEN")
        or os.getenv("OPENAI_COMPATIBLE_API_KEY")
        or ""
    ).strip()


def _llm_proxy_url() -> str:
    return os.getenv("LLM_PROXY_URL", "").rstrip("/")


def _llm_proxy_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = os.getenv("LLM_PROXY_TOKEN", "")
    if token:
        headers["X-Internal-Token"] = token
    return headers


def _load_payload(input_json: str | None) -> Any:
    if input_json:
        with open(input_json, "r", encoding="utf-8") as fh:
            return json.load(fh)
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("No JSON input supplied. Use --input-json or pipe JSON to stdin.")
    return json.loads(raw)


def _find_extracted(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    extracted = payload.get("extracted")
    if isinstance(extracted, dict):
        return extracted
    if isinstance(payload.get("images"), list):
        return payload
    return None


def _find_source_path(payload: Any, extracted: dict[str, Any] | None) -> str:
    if isinstance(payload, dict):
        ingested_doc = payload.get("ingested_doc")
        if isinstance(ingested_doc, dict) and ingested_doc.get("source_path"):
            return str(ingested_doc["source_path"])
    if isinstance(extracted, dict):
        ingested = extracted.get("ingested")
        if isinstance(ingested, dict) and ingested.get("source_path"):
            return str(ingested["source_path"])
    return ""


def _response_preview(raw: bytes, max_chars: int = 500) -> str:
    return raw.decode("utf-8", errors="replace").strip()[:max_chars]


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()
    return str(content).strip() if content is not None else ""


def _get_message_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return _content_to_text(message.get("content"))


def _get_sse_message_content(raw: bytes) -> str:
    parts: list[str] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            content = delta.get("content") if "content" in delta else message.get("content")
            text = _content_to_text(content)
            if text:
                parts.append(text)
    return "".join(parts).strip()


def _is_invalid_vision_response(description: str) -> bool:
    text = (description or "").strip().lower()
    if not text:
        return False
    invalid_markers = (
        "i don't see any image attached",
        "i do not see any image attached",
        "no image attached",
        "could not see an image",
        "can't see an image",
        "cannot see an image",
        "error generating response",
        "error generating response from image",
        "couldn't generate a response",
        "could not generate a response",
    )
    return any(marker in text for marker in invalid_markers)


def _extract_image_for_vision(source_path: str, xref: int, fallback_mime_type: str = "image/png") -> tuple[str, str]:
    from core import pdf_backend as fitz

    doc = fitz.open(source_path)
    try:
        info = doc.extract_image(int(xref))
        image_bytes = info.get("image") or b""
        ext = (info.get("ext") or "").lower()
        mime_type = _mime_type_for_ext(ext) if ext else fallback_mime_type
        return base64.b64encode(image_bytes).decode("utf-8"), mime_type
    finally:
        doc.close()


def describe_one_image_gemini(
    *,
    base64_value: str,
    mime_type: str,
    model: str,
    prompt: str = PROMPT,
    timeout: int = 120,
) -> str:
    proxy_url = _llm_proxy_url()
    if not proxy_url:
        return describe_one_image(
            base64_value=base64_value,
            mime_type=mime_type,
            model=model,
            prompt=prompt,
            timeout=timeout,
        )

    body = {
        "provider": "gemini",
        "prompt": prompt,
        "image_b64": base64_value,
        "mime_type": mime_type,
        "model": model,
    }
    encoded = json.dumps(body).encode("utf-8")
    req = request.Request(
        f"{proxy_url}/llm/generate-image",
        data=encoded,
        headers=_llm_proxy_headers(),
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except error.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        raise VisionResponseError(
            f"HTTPError: {exc.code} {exc.reason}",
            _response_preview(raw),
        ) from exc

    preview = _response_preview(raw)
    try:
        response = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise VisionResponseError(
            f"JSONDecodeError: {exc}",
            preview,
        ) from exc
    return str(response.get("text") or "").strip()


def describe_one_image(
    *,
    base64_value: str,
    mime_type: str,
    model: str,
    prompt: str = PROMPT,
    temperature: float = 0.2,
    max_tokens: int = 300,
    timeout: int = 120,
) -> str:
    data_url = f"data:{mime_type};base64,{base64_value}"
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    encoded = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    # The platform gateway authenticates agent calls with X-Internal-Token
    # (matches app/core/llm_handler.py OpenAIClient). Without it the gateway
    # returns "Error generating response" for the vision turn, which this
    # script otherwise records as vision_model_did_not_process_image.
    internal_token = _internal_token()
    if internal_token:
        headers["X-Internal-Token"] = internal_token
    req = request.Request(
        f"{_base_url()}/chat/completions",
        data=encoded,
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except error.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        raise VisionResponseError(
            f"HTTPError: {exc.code} {exc.reason}",
            _response_preview(raw),
        ) from exc

    preview = _response_preview(raw)
    try:
        response = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        sse_content = _get_sse_message_content(raw)
        if sse_content:
            return sse_content
        raise VisionResponseError(
            f"JSONDecodeError: {exc}",
            preview,
        ) from exc
    return _get_message_content(response)


def _describe_image_fields(
    *,
    source_path: str,
    xref: int,
    mime_type: str,
    model: str,
    provider: str,
    timeout: int,
) -> dict[str, Any]:
    """Compute the description fields for ONE image.

    Pure with respect to shared state — it only reads its inputs and returns a
    dict of fields to merge onto the image record. Safe to run inside a thread
    pool because it never mutates anything shared. Per-image failures are
    captured as ``failed``/``empty_response`` status fields rather than raised.
    """
    uses_gemini_route = provider == "gemini" or model.lower().startswith("gemini")
    proxy_url = _llm_proxy_url()
    route_url = f"{proxy_url}/llm/generate-image" if uses_gemini_route and proxy_url else f"{_base_url()}/chat/completions"
    route_kind = "gemini_proxy" if uses_gemini_route and proxy_url else "openai_compatible_image_url"

    fields: dict[str, Any] = {
        "description_route": route_kind,
        "description_endpoint": route_url,
        "description_model": model,
        "description_cached": False,
        # Keys that may need to be cleared on success/empty; None => pop.
        "description_error": None,
        "description_response_preview": None,
    }
    try:
        base64_value, detected_mime_type = _extract_image_for_vision(source_path, int(xref), mime_type)
        effective_mime_type = detected_mime_type or mime_type
        if uses_gemini_route:
            description = describe_one_image_gemini(
                base64_value=base64_value,
                mime_type=effective_mime_type,
                model=model,
                timeout=timeout,
            )
        else:
            description = describe_one_image(
                base64_value=base64_value,
                mime_type=effective_mime_type,
                model=model,
                timeout=timeout,
            )
        if description and not _is_invalid_vision_response(description):
            fields["description"] = description
            fields["description_status"] = "success"
        elif description:
            fields["description_status"] = "failed"
            fields["description_error"] = "vision_model_did_not_process_image"
            fields["description_response_preview"] = description[:500]
        else:
            fields["description_status"] = "empty_response"
            fields["description_error"] = "vision_model_returned_empty_response"
    except Exception as exc:  # noqa: BLE001 — never let one image abort the batch
        fields["description_status"] = "failed"
        fields["description_error"] = f"{type(exc).__name__}: {exc}"
        response_preview = getattr(exc, "response_preview", "")
        if response_preview:
            fields["description_response_preview"] = response_preview
    return fields


def _apply_fields(image: dict[str, Any], fields: dict[str, Any], *, cached: bool) -> None:
    """Merge computed description ``fields`` onto an image record.

    ``None`` values mean "remove this key" (mirrors the original behavior where
    success cleared ``description_error`` / ``description_response_preview``).
    Preserves any existing ``description`` when the call produced none.
    """
    for key, value in fields.items():
        if value is None:
            image.pop(key, None)
        else:
            image[key] = value
    if not image.get("description"):
        image["description"] = image.get("description") or ""
    image["description_cached"] = cached
    image.pop("base64", None)


def enrich_images(
    payload: Any,
    *,
    describe_images: bool,
    model: str,
    provider: str = "gemini",
    max_images: int | None = None,
    timeout: int = 120,
    workers: int = DEFAULT_WORKERS,
) -> Any:
    result = deepcopy(payload)
    extracted = _find_extracted(result)
    if not extracted:
        return result

    images = extracted.get("images") or []
    if isinstance(images, list):
        for image in images:
            if isinstance(image, dict):
                image.pop("base64", None)

    if not describe_images:
        return result

    if not isinstance(images, list) or not images:
        return result

    source_path = _find_source_path(result, extracted)
    if not source_path:
        return result

    # ── Phase 1 (serial, cheap): group describable images by sha256. ──────────
    # Dedup is order-independent: the lowest-index image for each sha is the
    # "representative" that actually calls the model; the rest reuse its result.
    # ``max_images`` caps the number of UNIQUE images described.
    unique_order: list[str] = []                  # shas in first-seen order
    representative: dict[str, int] = {}           # sha -> representative image index
    dup_indices: dict[str, list[int]] = {}        # sha -> duplicate image indices
    for idx, image in enumerate(images):
        if not isinstance(image, dict):
            continue
        if image.get("xref") is None:
            continue
        sha = str(image.get("sha256") or "") or f"__noidx_{idx}"
        if sha not in representative:
            if max_images is not None and len(unique_order) >= max_images:
                continue
            representative[sha] = idx
            dup_indices[sha] = []
            unique_order.append(sha)
        else:
            dup_indices[sha].append(idx)

    if not unique_order:
        return result

    # ── Phase 2 (parallel): describe each unique image. No shared writes. ─────
    def _task(sha: str) -> tuple[str, dict[str, Any]]:
        rep_image = images[representative[sha]]
        fields = _describe_image_fields(
            source_path=source_path,
            xref=int(rep_image.get("xref")),
            mime_type=rep_image.get("mime_type") or "image/png",
            model=model,
            provider=provider,
            timeout=timeout,
        )
        return sha, fields

    effective_workers = max(1, min(workers, MAX_WORKERS, len(unique_order)))
    fields_by_sha: dict[str, dict[str, Any]] = {}
    if effective_workers == 1:
        for sha in unique_order:
            s, fields = _task(sha)
            fields_by_sha[s] = fields
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as pool:
            for s, fields in pool.map(_task, unique_order):
                fields_by_sha[s] = fields

    # ── Phase 3 (serial merge / single-writer): only the main thread mutates. ─
    for sha in unique_order:
        fields = fields_by_sha.get(sha, {})
        _apply_fields(images[representative[sha]], fields, cached=False)
        for dup_idx in dup_indices.get(sha, []):
            _apply_fields(images[dup_idx], dict(fields), cached=True)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich DSLAR extracted image records with vision model descriptions.")
    parser.add_argument("--input-json", help="Path to the JSON payload. If omitted, JSON is read from stdin.")
    parser.add_argument("--output-json", help="Path to write the enriched JSON. If omitted, JSON is written only to stdout.")
    parser.add_argument("--describe-images", default="false", help="Whether image descriptions are requested.")
    parser.add_argument("--provider", default="gemini", choices=["gemini", "openai-compatible"], help="Vision provider payload format.")
    parser.add_argument("--model", default=os.getenv("LOCAL_LLM_MODEL", ""), help="Vision-capable model ID.")
    parser.add_argument("--max-images", type=int, default=None, help="Optional cap on images to enrich.")
    parser.add_argument("--timeout", type=int, default=120, help="Per-image request timeout in seconds.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Parallel vision-call workers (default {DEFAULT_WORKERS}, clamped 1-{MAX_WORKERS}).")
    args = parser.parse_args()

    model = args.model.strip()
    if not model:
        raise ValueError("A vision-capable model must be supplied with --model or LOCAL_LLM_MODEL.")

    payload = _load_payload(args.input_json)
    enriched = enrich_images(
        payload,
        describe_images=_parse_bool(args.describe_images),
        model=model,
        provider=args.provider,
        max_images=args.max_images,
        timeout=args.timeout,
        workers=args.workers,
    )
    output = json.dumps(enriched, ensure_ascii=False)
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as fh:
            fh.write(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
