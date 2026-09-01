# SPDX-License-Identifier: Apache-2.0
"""
Presenton proxy router — wraps the open-source Presenton PPT engine.

All routes go through the compliance gate before reaching Presenton.
Presenton runs as a Docker container at PRESENTON_URL (default :5001).
It calls back to our gateway's OpenAI-compat endpoint for LLM inference,
so all model routing (Claude → GPT-5.2 → Ollama) + circuit breaker fallback
is handled transparently by our gateway.

Endpoints
─────────
  POST /ppt/outline                → Claude-generated editable slide outline
  POST /ppt/generate               → Compliance gate → enqueue Presenton job → {job_id}
  GET  /ppt/status/{job_id}        → Poll Redis for job result
  GET  /ppt/download/{job_id}      → Proxy file download from Presenton
  GET  /ppt/themes                 → Available Presenton template catalogue
"""

import json
import os
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional

from auth.dependencies import get_current_user
from agents.compliance_engine import compliance_engine
from core.config import PRESENTON_URL, PRESENTON_USER, PRESENTON_PASSWORD, PPT_LLM_MODEL, PLATFORM_NAME as _PLATFORM_NAME

# Volume mount: container /app_data  →  host $HOME/.ainxt/presenton_data
_PRESENTON_DATA_DIR = os.path.expanduser(
    os.getenv("PRESENTON_DATA_DIR", "~/.ainxt/presenton_data")
)
from core.job_queue import Q_DOC, enqueue_job
from core.logger import logger
from core.prompt_sanitizer import sanitize
from core.security_validation import (
    validate_presenton_outline_request,
    validate_presenton_generate_request,
    _flatten_errors,
)
from models.model_router import model_router

router = APIRouter(tags=["ppt"])

# ── Theme catalogue — Presenton built-ins + AiNxt brand ───────────────────────
_THEMES = [
    {
        "id":          "modern",
        "name":        "Modern (Recommended)",
        "description": "Charts, metrics tables, and data-rich layouts",
        "preview":     "light",
        "color":       "#1A73E8",
    },
    {
        "id":          "general",
        "name":        "Corporate",
        "description": "Professional layout with chart and metrics slides",
        "preview":     "dark",
        "color":       "#1A2744",
    },
    {
        "id":          "standard",
        "name":        "Standard",
        "description": "Classic clean structured layout",
        "preview":     "light",
        "color":       "#374151",
    },
    {
        "id":          "swift",
        "name":        "Swift",
        "description": "Fast, dynamic, bold visual style",
        "preview":     "dark",
        "color":       "#7C3AED",
    },
]


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class OutlineRequest(BaseModel):
    prompt: str
    n_slides: int = 8


class GenerateRequest(BaseModel):
    prompt: str
    slides_markdown: Optional[list[str]] = None   # user-edited outline from Step 1
    template: str = "general"
    n_slides: int = 8
    tone: str = "professional"
    language: str = "English"
    verbosity: str = "standard"
    include_table_of_contents: bool = False
    export_as: str = "pptx"
    chat_id: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compliance_check(text: str) -> None:
    try:
        result = compliance_engine.validate_input(text[:4000])
        if result.get("blocked"):
            raise HTTPException(
                status_code=403,
                detail="Content blocked by compliance policy",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"presenton_router: compliance fail-open: {exc}")


_PRESENTON_AUTH = (PRESENTON_USER, PRESENTON_PASSWORD)


def _presenton_alive() -> bool:
    try:
        r = httpx.get(f"{PRESENTON_URL}/api/v1/auth/status", timeout=3.0)
        return r.status_code < 500
    except Exception:
        return False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/ppt/themes")
def list_themes(_user=Depends(get_current_user)):
    """Return the available presentation theme catalogue."""
    return {"themes": _THEMES}


@router.post("/ppt/outline")
def generate_outline(req: OutlineRequest, _user=Depends(get_current_user)):
    """
    Use Claude Sonnet (→ GPT-5.2 → Ollama fallback) to produce an editable
    slide outline the user reviews in Step 1 of the wizard before generating.

    Returns: {title, slides: [{title, bullets[]}]}
    """
    _ok, _errs, _san = validate_presenton_outline_request(req)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors(_errs))
    req.prompt = _san["prompt"]

    _compliance_check(req.prompt)

    prompt = (
        f"You are a world-class presentation designer for AiNxt "
        f"({_PLATFORM_NAME}). "
        f"Create a rich, visually compelling slide outline for: {sanitize(req.prompt)}\n\n"
        f"Respond with ONLY valid JSON — no markdown fences, no explanation.\n\n"
        f"JSON schema:\n"
        f'{{"title":"<concise title>","slides":[\n'
        f'  {{"title":"<slide title>","bullets":["<point>","<point>"],"chart":{{'
        f'"type":"bar|pie|line|donut|none","title":"<chart title>",'
        f'"labels":["<label1>","<label2>"],"values":[<num1>,<num2>]}},'
        f'"stats":[{{"label":"<metric>","value":"<value with unit>","delta":"<+X% YoY>"}}]'
        f'}}\n'
        f']}}\n\n'
        f"Rules:\n"
        f"- Exactly {req.n_slides} slides. Slide 1 = title/overview, last = next steps.\n"
        f"- For EVERY slide decide: include chart OR stats OR neither (set chart.type='none', stats=[]).\n"
        f"- At least {max(2, req.n_slides // 3)} slides must have a chart (bar, pie, line, or donut).\n"
        f"- At least {max(1, req.n_slides // 4)} slides must have stats (key metrics with real numbers).\n"
        f"- Charts: use real, accurate figures for the subject (e.g. volumes, share %).\n"
        f"- Stats: bold memorable numbers with their unit (e.g. '14B+', '46%', '300M users').\n"
        f"- Bullets: 3-5 per slide, ≤14 words each, factual and precise.\n"
        # The subject comes from the user's own prompt. This rule used to read
        # "Use Indian financial context where relevant (UPI, RuPay, Bharat, ₹)",
        # which pushed every deck towards payments framing and rupee figures
        # whatever the user had actually asked for.
        f"- Match the domain, units and currency to the requested subject.\n"
        f"- Output raw JSON only — absolutely no ```json``` fences or explanation."
    )

    try:
        raw = model_router.generate(sanitize(prompt), model_hint="complex")
        raw = (raw or "").strip()
        raw = re.sub(r"^```[a-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw.strip())
        outline = json.loads(raw)
        return outline
    except Exception as exc:
        logger.error(f"presenton_router: outline generation failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Outline generation failed: {exc}")


@router.post("/ppt/generate")
def generate_presentation(req: GenerateRequest, user=Depends(get_current_user)):
    """
    Compliance gate → enqueue Presenton generation job → return {job_id}.
    Poll GET /ppt/status/{job_id} for completion.
    """
    _ok, _errs, _san = validate_presenton_generate_request(req)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors(_errs))
    req.prompt = _san["prompt"]
    req.template = _san["template"]

    _compliance_check(req.prompt)

    if not _presenton_alive():
        raise HTTPException(
            status_code=503,
            detail=(
                "Presentation engine is not running. "
                "Start it with: pm2 start presenton"
            ),
        )

    job_id = enqueue_job(
        "workers.presenton_worker.generate_ppt_job",
        {
            "job_id":                    None,   # worker sets from rq job id
            "prompt":                    sanitize(req.prompt),
            "slides_markdown":           req.slides_markdown or [],
            "template":                  req.template,
            "n_slides":                  req.n_slides,
            "tone":                      req.tone,
            "language":                  req.language,
            "verbosity":                 req.verbosity,
            "include_table_of_contents": req.include_table_of_contents,
            "export_as":                 req.export_as,
            "chat_id":                   req.chat_id,
            "user_id":                   getattr(user, "id", None) or getattr(user, "user_id", "unknown"),
        },
        queue_name=Q_DOC,
        timeout=300,
        retry_count=1,
    )

    return {"job_id": job_id, "status": "queued"}


@router.get("/ppt/status/{job_id}")
def presentation_status(job_id: str, _user=Depends(get_current_user)):
    """
    Poll the result KV for the presenton job result.
    Returns {status, download_url, edit_url, filename} when done.
    Backend selected via REDIS_CLIENT_CONFIG_DB6.
    """
    from core.config import RDB_STREAM
    from core.kv import get_kv

    r = get_kv(RDB_STREAM, decode_responses=True)
    raw = r.get(f"ppt:result:{job_id}")
    if not raw:
        return {"status": "processing"}

    try:
        data = json.loads(raw)
    except Exception:
        return {"status": "error", "error": "Invalid result payload"}

    if data.get("status") == "done":
        return {
            "status":       "done",
            "download_url": f"/ainxt/v1/api/ppt/download/{job_id}",
            "edit_url":     data.get("edit_url", ""),
            "filename":     data.get("filename", "presentation.pptx"),
            "cost_usd":     data.get("cost_usd", 0.0),
            "cost_breakdown": {
                "text_usd":    data.get("text_cost_usd", 0.0),
                "image_usd":   data.get("image_cost_usd", 0.0),
                "image_count": data.get("image_count", 0),
            },
        }
    if data.get("status") == "error":
        return {"status": "error", "error": data.get("error", "Unknown error")}

    return {"status": "processing"}


@router.get("/ppt/download/{job_id}")
def download_presentation(job_id: str, _user=Depends(get_current_user)):
    """Proxy the generated file from Presenton's local storage → client.

    Result metadata lives in the stream KV (DB=6).
    """
    from core.config import RDB_STREAM
    from core.kv import get_kv

    r = get_kv(RDB_STREAM, decode_responses=True)
    raw = r.get(f"ppt:result:{job_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Presentation not found or not ready")

    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=500, detail="Corrupt result record")

    if data.get("status") != "done":
        raise HTTPException(status_code=409, detail="Presentation is not ready yet")

    presenton_path = data.get("presenton_path", "")
    filename       = data.get("filename", "presentation.pptx")
    export_as      = data.get("export_as", "pptx")

    mime = (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        if export_as == "pptx" else "application/pdf"
    )

    # presenton_path is the container-internal path: /app_data/exports/file.pptx
    # Convert to host filesystem path via the volume mount.
    if presenton_path.startswith("/app_data/"):
        relative = presenton_path[len("/app_data/"):]
        host_path = os.path.join(_PRESENTON_DATA_DIR, relative)
    else:
        host_path = presenton_path

    if os.path.isfile(host_path):
        return FileResponse(
            host_path,
            media_type=mime,
            filename=filename,
        )

    # Fallback: proxy via Presenton HTTP
    download_url = f"{PRESENTON_URL}{presenton_path}"

    def _stream():
        try:
            with httpx.stream("GET", download_url, auth=_PRESENTON_AUTH, timeout=60.0) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=8192):
                    yield chunk
        except Exception as exc:
            logger.error(f"presenton_router: stream download failed: {exc}")

    return StreamingResponse(
        _stream(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )