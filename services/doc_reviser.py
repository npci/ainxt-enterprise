# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SHARED DOCUMENT REVISE ENGINE
#
# "make the intro shorter", "add a section on X", "change the tone", "convert
# that to PDF" → load the PREVIOUS version's editable source, apply the change
# with the authoring model, and rebuild as a NEW VERSION (same artifact_id,
# version+1). No blind regeneration.
#
# This mirrors the Buddy MCP revise (connectors/mcp_bridge._revise_artifact) but
# lives as a standalone service the Chat REST path calls — the MCP path is left
# untouched per design (minimal duplicated logic here).
#
# Authoring/edit = cloud Claude Sonnet ("complex"); reference resolution = fast
# local model (inside services.doc_context).
# ============================================================

from __future__ import annotations

import re
import uuid as _uuid
from typing import Optional

from core.logger import logger


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def revise(
    *, artifact_id: str, instruction: str, user_id: str,
    chat_id: Optional[str] = None, target_format: Optional[str] = None,
    user_model_hint: str = "auto",
) -> dict:
    """Revise the latest version of `artifact_id`.

    Returns {"ok": True, "job_id", "artifact_id", "version", "format", "title"}
    on success (a rebuild job is enqueued — poll /docs/job/{job_id}/status), or
    {"ok": False, "error": "..."} on failure. Never raises."""
    artifact_id = (artifact_id or "").strip()
    instruction = (instruction or "").strip()
    if not artifact_id or not instruction:
        return {"ok": False, "error": "revise needs artifact_id and an instruction."}

    try:
        from services.doc_context import load_latest_source
        ref = load_latest_source(artifact_id, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[doc_reviser] load failed: {exc}")
        return {"ok": False, "error": "Could not load the document to revise."}

    if not ref:
        return {"ok": False, "error": f"No document found for artifact_id {artifact_id!r}."}

    source = (ref.content_md or "").strip()
    if not source:
        return {"ok": False,
                "error": "That version has no stored editable source — regenerate it first."}

    # Coherent version chain: if the source was a ONE-SHOT doc (artifact_id was
    # NULL, so its handle == its row id), backfill artifact_id on that original
    # row to its own id so v1 and the new v2 share the same artifact_id.
    try:
        from services.doc_context import ensure_artifact_id
        artifact_id = ensure_artifact_id(ref.doc_id, user_id) or artifact_id
    except Exception:  # noqa: BLE001
        pass

    # A format change ("convert that to PDF") keeps the same source but retargets.
    out_fmt = (target_format or ref.format or "docx").lower()
    new_version = int(ref.version or 1) + 1

    # ── Apply the natural-language edit to the SOURCE (authoring model) ──
    # Capture token/cost meta (return_meta=True) so the revision's usage is
    # reported and billed. Without this, the downstream render-only job has no
    # LLM call and reports 0 tokens for the revise turn.
    edit_meta: dict = {}
    try:
        from models.model_router import model_router
        _res = model_router.generate(
            "You are an expert editor revising an existing document. The document's "
            "current Markdown source is below. Apply the requested change faithfully and "
            "return ONLY the FULL revised Markdown — no commentary, no code fences. "
            "Preserve everything the user did not ask to change.\n\n"
            f"CHANGE REQUESTED: {instruction}\n\n"
            f"CURRENT DOCUMENT (\"{ref.title}\"):\n{source[:80000]}",
            model_hint="complex",   # cloud authoring model — quality
            return_meta=True,
        )
        if isinstance(_res, dict):
            edited = (_res.get("text") or "").strip()
            edit_meta = _res.get("meta") or {}
        else:
            # Defensive: older/alt return shape (plain string)
            edited = (_res or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[doc_reviser] edit LLM failed: {exc}")
        return {"ok": False, "error": "The revision model failed — try rephrasing the change."}

    edited = _strip_fences(edited)
    if not edited:
        return {"ok": False, "error": "The revision produced no output — try rephrasing."}

    # ── Enqueue a rebuild as a NEW VERSION of the same artifact ──
    try:
        from core.job_queue import Q_DOC, enqueue_job
        job_id = str(_uuid.uuid4())
        payload = {
            "job_id":      job_id,
            "format":      out_fmt,
            "title":       ref.title,
            "content_md":  edited,
            "question":    instruction,
            "user_id":     str(user_id),
            "chat_id":     chat_id or None,
            "artifact_id": artifact_id,     # SAME logical doc
            "version":     new_version,     # bumped
            "user_model_hint": user_model_hint or "auto",
            # Carry the revision LLM's token/cost meta downstream. generate_doc_job
            # renders content_md verbatim (no LLM call of its own), so without this
            # the revise turn would report 0 tokens and skip budget accounting.
            "llm_meta":    edit_meta or None,
        }
        enqueue_job(
            "workers.doc_worker_agent.generate_doc_job",
            payload, queue_name=Q_DOC, timeout=1800, retry_count=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[doc_reviser] enqueue failed: {exc}")
        return {"ok": False, "error": "Could not queue the revision."}

    return {"ok": True, "job_id": job_id, "artifact_id": artifact_id,
            "version": new_version, "format": out_fmt, "title": ref.title}
