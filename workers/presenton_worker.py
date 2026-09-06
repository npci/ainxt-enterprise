# SPDX-License-Identifier: MIT
"""
Presenton RQ worker — calls the Presenton Docker API to generate a PPTX/PDF.

Flow:
  1. Compliance gate on prompt
  2. POST to Presenton /api/v1/ppt/presentation/generate (blocking, up to 240s)
  3. Store result in Redis ppt:result:{job_id} (TTL 24h)

The worker is queued onto Q_DOC and runs inside the existing doc workers
(python workers/start_workers.py --doc --n 5).
"""

import json
import os
import uuid as _uuid_mod

import httpx

from core.config import (
    PRESENTON_URL, PRESENTON_USER, PRESENTON_PASSWORD,
    RDB_STREAM,
)
from core.kv import get_kv
from core.logger import logger

# DB=6 — presenton result delivery. Backend selected via REDIS_CLIENT_CONFIG_DB6.
_R = get_kv(RDB_STREAM, decode_responses=True)

RESULT_TTL = 86400   # 24 h


def generate_ppt_job(payload: dict) -> None:
    """
    RQ job entry point.

    payload keys:
      prompt                    str   — sanitised user prompt
      slides_markdown           list  — user-edited outline (optional)
      template                  str   — Presenton template id
      n_slides                  int   — number of slides
      tone                      str   — professional / casual / educational …
      language                  str   — default "English"
      verbosity                 str   — concise / standard / text-heavy
      include_table_of_contents bool
      export_as                 str   — pptx | pdf
      chat_id                   str   — originating chat (nullable)
      user_id                   str   — requesting user id
    """
    # Resolve job_id from the RQ job context when available
    try:
        from rq import get_current_job
        rq_job = get_current_job()
        job_id = rq_job.id if rq_job else str(_uuid_mod.uuid4())
    except Exception:
        job_id = str(_uuid_mod.uuid4())

    prompt     = payload.get("prompt", "")
    export_as  = payload.get("export_as", "pptx")
    template   = payload.get("template", "general")
    n_slides   = int(payload.get("n_slides", 8))
    tone       = payload.get("tone", "professional")
    language   = payload.get("language", "English")
    verbosity  = payload.get("verbosity", "standard")
    include_toc = bool(payload.get("include_table_of_contents", False))
    slides_md  = payload.get("slides_markdown") or []

    # ── Compliance gate ──────────────────────────────────────────
    try:
        from agents.compliance_engine import compliance_engine as _ce
        chk = _ce.validate_input(prompt[:4000])
        if chk.get("blocked"):
            _fail(job_id, "Content blocked by compliance policy")
            return
    except Exception as ce_err:
        logger.warning(f"presenton_worker: compliance fail-open: {ce_err}")

    # ── Build Presenton request payload ─────────────────────────
    # instructions tells Presenton's LLM to favour chart/stats/diagram layouts
    instructions = (
        "Maximise visual richness: use chart slides (bar, line, pie, donut) for any "
        "numerical or comparative data; use metrics/KPI slides for key numbers; use "
        "workflow or timeline slides for processes; use table slides for structured "
        "comparisons. Avoid using plain bullet-list slides when a chart or metric layout "
        "better represents the data. Every slide must have a distinct, compelling visual element."
    )

    presenton_body: dict = {
        "content":                   prompt,
        "n_slides":                  n_slides,
        "tone":                      tone,
        "language":                  language,
        "verbosity":                 verbosity,
        "template":                  template,
        "include_table_of_contents": include_toc,
        "export_as":                 export_as,
        "instructions":              instructions,
    }
    if slides_md:
        # Convert outline dicts to rich markdown strings Presenton understands
        presenton_body["slides_markdown"] = [
            _outline_to_markdown(s) if isinstance(s, dict) else str(s)
            for s in slides_md
        ]

    logger.info(
        f"presenton_worker: calling Presenton for job {job_id} "
        f"({n_slides} slides, template={template}, export={export_as})"
    )

    # ── Call Presenton (blocking — up to 240s) ───────────────────
    try:
        resp = httpx.post(
            f"{PRESENTON_URL}/api/v1/ppt/presentation/generate",
            json=presenton_body,
            auth=(PRESENTON_USER, PRESENTON_PASSWORD),
            timeout=240.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError:
        _fail(job_id, "Presentation engine is not reachable. Start it with: pm2 start presenton")
        return
    except httpx.TimeoutException:
        _fail(job_id, "Presentation engine timed out after 240s")
        return
    except Exception as exc:
        logger.error(f"presenton_worker: Presenton API call failed for job {job_id}: {exc}")
        _fail(job_id, f"Presentation engine error: {exc}")
        return

    # ── Extract result ───────────────────────────────────────────
    from tools.doc_generator import smart_filename as _smart_filename
    presentation_id = data.get("presentation_id", "")
    path            = data.get("path", "")
    edit_path       = data.get("edit_path", "")
    _base    = _smart_filename(title="", question=prompt, fmt_ext=export_as)
    filename = f"{_base}.{export_as}"

    if not presentation_id:
        _fail(job_id, "Presenton returned no presentation_id")
        return

    # ── Publish result to Redis ──────────────────────────────────
    result = {
        "status":          "done",
        "presentation_id": presentation_id,
        "presenton_path":  path,
        "filename":        filename,
        "export_as":       export_as,
        "edit_url":        f"{PRESENTON_URL}{edit_path}" if edit_path else "",
    }
    _R.setex(f"ppt:result:{job_id}", RESULT_TTL, json.dumps(result))
    logger.info(
        f"presenton_worker: job {job_id} done — "
        f"{filename} (presentation_id={presentation_id})"
    )


def _outline_to_markdown(slide: dict) -> str:
    """Convert an outline slide dict (from /ppt/outline) to rich markdown for Presenton."""
    parts = []
    title = slide.get("title", "")
    if title:
        parts.append(f"# {title}")

    bullets = slide.get("bullets") or []
    if bullets:
        parts.extend(f"- {b}" for b in bullets)

    # Embed chart data as markdown table so Presenton's LLM can build a chart slide
    chart = slide.get("chart") or {}
    if chart.get("type") and chart.get("type") != "none" and chart.get("labels") and chart.get("values"):
        parts.append(f"\n**Chart ({chart['type']}): {chart.get('title', '')}**")
        parts.append("| Label | Value |")
        parts.append("|-------|-------|")
        for label, value in zip(chart["labels"], chart["values"]):
            parts.append(f"| {label} | {value} |")

    # Embed stats as a callout block
    stats = slide.get("stats") or []
    if stats:
        parts.append("\n**Key Metrics:**")
        for s in stats:
            delta = f" ({s['delta']})" if s.get("delta") else ""
            parts.append(f"- **{s['value']}** — {s['label']}{delta}")

    return "\n".join(parts)


def _fail(job_id: str, error: str) -> None:
    _R.setex(
        f"ppt:result:{job_id}",
        3600,
        json.dumps({"status": "error", "error": error}),
    )
    logger.error(f"presenton_worker: job {job_id} failed — {error}")