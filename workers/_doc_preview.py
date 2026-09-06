# SPDX-License-Identifier: MIT
"""Shared live-preview publisher for doc generation workers.

Both ``workers/doc_worker.py`` (binary formats) and
``workers/doc_worker_agent.py`` (MD format) stream LLM output and publish
incremental sections to Redis so the chat UI can render a CoWorker-style
materializing preview instead of an opaque spinner.

Redis key layout (DB=6 — same DB as doc:result and doc:progress):
    doc:live_preview:{job_id}   TTL 600s   JSON payload

Payload schema (kept stable for the frontend):
    {
      "title":      str,                    # current best title (may be empty)
      "domain":     str,                    # current best domain (may be empty)
      "sections": [
        {
          "heading": str,
          "content": str,                   # markdown body (already complete)
          "bullets": list[str],
          "callout": {"label": str, "text": str} | None,
        },
        ...
      ],
      "total_hint": int,                    # best-effort outline target; may grow
      "done":       bool,                   # True after the final section
    }

TTL matches ``doc:progress:{job_id}`` so behavior is consistent: a job that
runs longer than 10 minutes loses both progress and preview state.
"""

import json

from core.config import RDB_STREAM
from core.kv import get_kv
from core.logger import logger

_R = get_kv(RDB_STREAM, decode_responses=True)

PREVIEW_TTL = 600  # 10 min — matches doc:progress TTL


def publish_preview(
        job_id: str,
        *,
        title: str = "",
        domain: str = "",
        sections: list | None = None,
        total_hint: int = 0,
        done: bool = False,
) -> None:
    """Write a live-preview snapshot to Redis. Safe to call mid-stream.

    Failure modes are swallowed and logged — preview is a UX nicety, never
    a hard dependency of the generation pipeline.
    """
    try:
        _R.setex(
            f"doc:live_preview:{job_id}",
            PREVIEW_TTL,
            json.dumps({
                "title":      title or "",
                "domain":     domain or "",
                "sections":   sections or [],
                "total_hint": int(total_hint or 0),
                "done":       bool(done),
            }, ensure_ascii=False),
        )
    except Exception as exc:
        logger.warning(f"publish_preview: failed for job {job_id}: {exc}")


def make_preview_callbacks(job_id: str, total_hint: int = 6):
    """Build (on_title, on_section, publish_done) callbacks that incrementally
    publish a doc preview to Redis as the LLM streams sections. Used by both
    workers/doc_worker.py (binary formats) and workers/doc_worker_agent.py
    (MD flow). State lives in the closure so each callback publishes a
    cumulative snapshot.
    """
    state = {"title": "", "domain": "", "sections": []}

    def on_title(t: str, d: str) -> None:
        state["title"] = t or state["title"]
        state["domain"] = d or state["domain"]
        publish_preview(
            job_id, title=state["title"], domain=state["domain"],
            sections=state["sections"], total_hint=total_hint, done=False,
        )

    def on_section(sec: dict) -> None:
        state["sections"].append(sec)
        publish_preview(
            job_id, title=state["title"], domain=state["domain"],
            sections=state["sections"],
            total_hint=max(total_hint, len(state["sections"]) + 1),
            done=False,
        )

    def publish_done() -> None:
        publish_preview(
            job_id, title=state["title"], domain=state["domain"],
            sections=state["sections"], total_hint=len(state["sections"]),
            done=True,
        )

    return on_title, on_section, publish_done
