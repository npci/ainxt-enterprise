# SPDX-License-Identifier: Apache-2.0
"""Per-agent Sample Document endpoints.

Lets the end user attach an existing document (`.docx` / `.pptx` /
`.xlsx` / `.pdf`) to a document-generation agent as a **look-and-feel
reference**. The agent studies it and mimics its branding, fonts,
heading order, header/footer, logos, and slide/layout patterns while
staying free to adapt structure and content to the specific task.

There is exactly ONE sample per agent (product decision — see the
Agent Studio plan). Uploading a new sample replaces the previous one,
both on disk and in the ``agents.sample_doc`` JSONB blob.

Storage layout
--------------
::

    <GENERATED_FILES_DIR>/agent_samples/<agent_id>/sample.<ext>

The parent ``<agent_id>`` folder is destroyed when the sample is
cleared or replaced, so orphan files can't accumulate.

Runtime consumption
-------------------
When an agent with a sample is invoked, the engine surfaces three env
vars into the ``code_executor`` sandbox:

* ``SAMPLE_DOC_PATH`` — absolute path to the file on disk.
* ``SAMPLE_DOC_KIND`` — ``docx | pptx | xlsx | pdf``.
* ``SAMPLE_DOC_DIR``  — folder containing the file (so
  ``read_document`` can extract text from it via the file-path allow
  list).

A prompt block appended by ``app.core.skill_manifest.sample_doc_directive``
tells the LLM to treat those paths as guidance-not-constraint. All of
that plumbing is intentionally kept OUT of this module — this file only
owns the CRUD.
"""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Form
from fastapi.responses import FileResponse

from app.api.deps import require_access
from app.api.documents import _read_bounded
from app.core import workflow_repo
from app.models import AuthenticatedUser
from core.file_validator import validate_upload
from core.logger import logger

router = APIRouter()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Match /agent-runner/attachment's cap so any document the user could
# already pass through the chat attachment path is also accepted here.
_SAMPLE_DOC_MAX_BYTES = 25 * 1024 * 1024

# The four Office/PDF formats supported by the domain skills. These are
# the only kinds the LLM can meaningfully "open as base" — image /
# markdown / txt samples would just be text prompts and don't need
# their own slot.
_SAMPLE_DOC_ALLOWED_EXTENSIONS = frozenset({"docx", "pptx", "xlsx", "pdf"})

# Optional user notes cap. The notes are appended verbatim to the
# system prompt so an unbounded blob could balloon token cost; keep it
# to a couple of paragraphs.
_SAMPLE_DOC_NOTES_MAX_CHARS = 2000


def _samples_root() -> Path:
    """Base directory for all per-agent sample documents.

    Reuses ``GENERATED_FILES_DIR`` (already resolved and guaranteed to
    exist by ``main.py`` at startup) so samples share the same disk /
    volume mount as every other user-visible artifact. Falls back to
    ``ABStudio/tmp`` if the env var is somehow missing — matches the
    fallback pattern in ``documents._save_image_asset``.
    """
    generated_files_dir = os.environ.get("GENERATED_FILES_DIR", "")
    if not generated_files_dir:
        generated_files_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
        )
    root = Path(generated_files_dir).resolve() / "agent_samples"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _agent_sample_dir(agent_id: str) -> Path:
    """Folder for a single agent's sample. One file lives here at a
    time — uploading a replacement wipes the folder first.

    ``agent_id`` is validated at the DB layer (only agents this user
    owns are queryable), but we still sanitise the string here so a
    malicious id can't traverse (``..``) out of ``_samples_root``.
    """
    safe = "".join(c for c in (agent_id or "") if c.isalnum() or c in ("-", "_"))
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid agent id.")
    d = _samples_root() / safe
    return d


def _extension(filename: str) -> str:
    return Path(filename).suffix.lstrip(".").lower()


async def _load_owned_agent(agent_id: str, user_id: str) -> dict:
    agent = await workflow_repo.get_agent(agent_id, user_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/agent-runner/agents/{agent_id}/sample")
async def upload_agent_sample(
    agent_id: str,
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None),
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Upload (or replace) the sample document for ``agent_id``.

    On success the response echoes the metadata now stored on
    ``agents.sample_doc`` and includes a download URL the UI can hit
    to fetch the raw file back.
    """
    # 1. Ownership check — refuses 404 for foreign agents so we don't
    # leak existence.
    await _load_owned_agent(agent_id, current_user.id)

    filename = (file.filename or "sample").strip() or "sample"
    ext = _extension(filename)
    if ext not in _SAMPLE_DOC_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported sample document type '.{ext}'. Allowed: "
                + ", ".join(sorted(_SAMPLE_DOC_ALLOWED_EXTENSIONS))
            ),
        )

    # 2. Read + validate the payload. ``_read_bounded`` streams in
    # 1 MB chunks and 413s past the size cap so an oversized client
    # can't fully buffer before we notice.
    raw_bytes = await _read_bounded(file, _SAMPLE_DOC_MAX_BYTES)
    validation = validate_upload(
        filename=filename,
        content=raw_bytes,
        allowed_extensions=_SAMPLE_DOC_ALLOWED_EXTENSIONS,
        max_size_bytes=_SAMPLE_DOC_MAX_BYTES,
        caller="agent_sample_upload",
    )
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    # 3. Reset the per-agent folder. Removing it first drops any
    # previously-uploaded sample with a different extension so we
    # don't leave a stale ``.docx`` next to a fresh ``.pptx``.
    agent_dir = _agent_sample_dir(agent_id)
    if agent_dir.exists():
        try:
            shutil.rmtree(agent_dir)
        except OSError as exc:
            logger.warning(
                f"[AGENT] agent_sample: could not clear {agent_dir}: {exc}"
            )
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Canonical filename is ``sample.<ext>``: the sandbox prompt block
    # references the file only via SAMPLE_DOC_PATH, so a stable name
    # keeps logs and stack traces readable.
    dest = agent_dir / f"sample.{ext}"
    try:
        # Guard against a symlink-style traversal — the resolved path
        # must still live under the agent's folder.
        dest.resolve().relative_to(agent_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid sample destination.")
    with open(dest, "wb") as fh:
        fh.write(raw_bytes)

    notes_clean = (notes or "").strip()
    if len(notes_clean) > _SAMPLE_DOC_NOTES_MAX_CHARS:
        notes_clean = notes_clean[:_SAMPLE_DOC_NOTES_MAX_CHARS]

    sample_doc = {
        "path":        str(dest.resolve()),
        "kind":        ext,
        "name":        Path(filename).name,
        "size_bytes":  len(raw_bytes),
        "notes":       notes_clean,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    updated = await workflow_repo.set_agent_sample_doc(
        agent_id, current_user.id, sample_doc,
    )
    if not updated:
        # Ownership changed between the check and the write — clean up
        # the file we just wrote so it doesn't outlive the record.
        try:
            shutil.rmtree(agent_dir)
        except OSError:
            pass
        raise HTTPException(status_code=404, detail="Agent not found")

    logger.info(
        f"[AGENT] sample_doc uploaded: agent={agent_id} kind={ext} "
        f"size={len(raw_bytes)} user={current_user.id}"
    )
    return {
        **sample_doc,
        # UI convenience — points at the GET endpoint below so the
        # editor can offer a "Download" link for the current sample.
        "download_url": f"/agent-runner/agents/{quote(agent_id, safe='')}/sample/download",
    }


@router.get("/agent-runner/agents/{agent_id}/sample")
async def get_agent_sample(
    agent_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Return metadata about the agent's sample document (if any).

    Empty ``{}`` when no sample is attached — matches the shape stored
    on the row so the UI can render the same conditional block on
    load and after an upload.
    """
    agent = await _load_owned_agent(agent_id, current_user.id)
    sample = agent.get("sample_doc") or {}
    if not sample:
        return {}
    return {
        **sample,
        "download_url": f"/agent-runner/agents/{quote(agent_id, safe='')}/sample/download",
    }


@router.get("/agent-runner/agents/{agent_id}/sample/download")
async def download_agent_sample(
    agent_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Stream the raw sample file back for preview / re-download."""
    agent = await _load_owned_agent(agent_id, current_user.id)
    sample = agent.get("sample_doc") or {}
    path = sample.get("path") or ""
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="No sample attached")

    # Confirm the resolved path is still inside the samples root so a
    # tampered DB row can't turn this into an arbitrary-file read.
    try:
        Path(path).resolve().relative_to(_samples_root().resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Sample outside allowed root")

    display_name = sample.get("name") or Path(path).name
    return FileResponse(
        path=path,
        filename=display_name,
        media_type="application/octet-stream",
    )


@router.delete("/agent-runner/agents/{agent_id}/sample", status_code=204)
async def delete_agent_sample(
    agent_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Detach the sample from the agent and delete the file on disk.

    Idempotent — deleting a non-existent sample is a no-op success.
    """
    await _load_owned_agent(agent_id, current_user.id)
    # Wipe the on-disk copy first; if the DB update fails afterwards
    # we're left with an inconsistent record (metadata pointing at a
    # deleted file) rather than the opposite (metadata gone, file
    # stranded on disk forever).
    agent_dir = _agent_sample_dir(agent_id)
    if agent_dir.exists():
        try:
            shutil.rmtree(agent_dir)
        except OSError as exc:
            logger.warning(
                f"[AGENT] agent_sample: could not remove {agent_dir}: {exc}"
            )
    await workflow_repo.clear_agent_sample_doc(agent_id, current_user.id)
    return None


# ---------------------------------------------------------------------------
# Workflow-node scoped endpoints
# ---------------------------------------------------------------------------
# A workflow agent node can also attach a Sample Document. Unlike the
# per-agent slot above, workflow node metadata lives inside the workflow
# JSON blob (``node.data.sample_doc``) rather than a dedicated DB
# column, so these endpoints only own the on-disk file lifecycle — the
# frontend is responsible for writing the returned metadata into
# ``node.data.sample_doc`` via the workflow save path. Storage layout
# mirrors the agent version:
#
#     <GENERATED_FILES_DIR>/workflow_samples/<workflow_id>/<node_id>/sample.<ext>
#
# Ownership is enforced via ``workflow_repo.get_workflow(workflow_id,
# owner_user_id)`` — a foreign workflow yields 404 so we don't leak
# existence.


def _workflow_samples_root() -> Path:
    """Base directory for all workflow-node sample documents.

    Sibling of ``agent_samples`` under ``GENERATED_FILES_DIR`` — same
    disk / volume mount as every other user-visible artifact.
    """
    generated_files_dir = os.environ.get("GENERATED_FILES_DIR", "")
    if not generated_files_dir:
        generated_files_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
        )
    root = Path(generated_files_dir).resolve() / "workflow_samples"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _workflow_node_sample_dir(workflow_id: str, node_id: str) -> Path:
    """Folder for a single (workflow, node) sample. Sanitises both ids
    so a malicious value can't traverse (``..``) out of the samples
    root — same defence-in-depth as ``_agent_sample_dir``.
    """
    safe_wf = "".join(c for c in (workflow_id or "") if c.isalnum() or c in ("-", "_"))
    safe_node = "".join(c for c in (node_id or "") if c.isalnum() or c in ("-", "_"))
    if not safe_wf or not safe_node:
        raise HTTPException(status_code=400, detail="Invalid workflow / node id.")
    return _workflow_samples_root() / safe_wf / safe_node


async def _load_owned_workflow(workflow_id: str, user_id: str) -> dict:
    wf = await workflow_repo.get_workflow(workflow_id, user_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


@router.post("/agent-runner/workflows/{workflow_id}/nodes/{node_id}/sample")
async def upload_workflow_node_sample(
    workflow_id: str,
    node_id: str,
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None),
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Upload (or replace) the sample document for a workflow agent node.

    The response mirrors the agent-scoped endpoint. The caller (the
    workflow editor) is expected to write the returned metadata into
    ``node.data.sample_doc`` so the workflow serializer round-trips
    it on save.
    """
    await _load_owned_workflow(workflow_id, current_user.id)

    filename = (file.filename or "sample").strip() or "sample"
    ext = _extension(filename)
    if ext not in _SAMPLE_DOC_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported sample document type '.{ext}'. Allowed: "
                + ", ".join(sorted(_SAMPLE_DOC_ALLOWED_EXTENSIONS))
            ),
        )

    raw_bytes = await _read_bounded(file, _SAMPLE_DOC_MAX_BYTES)
    validation = validate_upload(
        filename=filename,
        content=raw_bytes,
        allowed_extensions=_SAMPLE_DOC_ALLOWED_EXTENSIONS,
        max_size_bytes=_SAMPLE_DOC_MAX_BYTES,
        caller="workflow_node_sample_upload",
    )
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    node_dir = _workflow_node_sample_dir(workflow_id, node_id)
    if node_dir.exists():
        try:
            shutil.rmtree(node_dir)
        except OSError as exc:
            logger.warning(
                f"[AGENT] workflow_node_sample: could not clear {node_dir}: {exc}"
            )
    node_dir.mkdir(parents=True, exist_ok=True)

    dest = node_dir / f"sample.{ext}"
    try:
        dest.resolve().relative_to(node_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid sample destination.")
    with open(dest, "wb") as fh:
        fh.write(raw_bytes)

    notes_clean = (notes or "").strip()
    if len(notes_clean) > _SAMPLE_DOC_NOTES_MAX_CHARS:
        notes_clean = notes_clean[:_SAMPLE_DOC_NOTES_MAX_CHARS]

    sample_doc = {
        "path":        str(dest.resolve()),
        "kind":        ext,
        "name":        Path(filename).name,
        "size_bytes":  len(raw_bytes),
        "notes":       notes_clean,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        f"[AGENT] sample_doc uploaded (workflow-node): "
        f"workflow={workflow_id} node={node_id} kind={ext} "
        f"size={len(raw_bytes)} user={current_user.id}"
    )
    return {
        **sample_doc,
        "download_url": (
            f"/agent-runner/workflows/{quote(workflow_id, safe='')}/nodes/"
            f"{quote(node_id, safe='')}/sample/download"
        ),
    }


@router.get(
    "/agent-runner/workflows/{workflow_id}/nodes/{node_id}/sample/download"
)
async def download_workflow_node_sample(
    workflow_id: str,
    node_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Stream the raw sample file back for the workflow node."""
    await _load_owned_workflow(workflow_id, current_user.id)

    node_dir = _workflow_node_sample_dir(workflow_id, node_id)
    # There's exactly one ``sample.<ext>`` per (workflow, node). Scan
    # the folder to find the extension so a caller doesn't need to
    # know it.
    if not node_dir.is_dir():
        raise HTTPException(status_code=404, detail="No sample attached")
    matches = [p for p in node_dir.iterdir()
               if p.is_file() and p.name.startswith("sample.")]
    if not matches:
        raise HTTPException(status_code=404, detail="No sample attached")
    path = matches[0]

    # Defence-in-depth: confirm the resolved path is still inside the
    # workflow samples root.
    try:
        path.resolve().relative_to(_workflow_samples_root().resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Sample outside allowed root")

    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/octet-stream",
    )


@router.delete(
    "/agent-runner/workflows/{workflow_id}/nodes/{node_id}/sample",
    status_code=204,
)
async def delete_workflow_node_sample(
    workflow_id: str,
    node_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Remove the file from disk. The frontend is responsible for
    clearing ``node.data.sample_doc`` in the workflow JSON afterwards
    (via ``updateNodeData(node_id, {sample_doc: {}})``).

    Idempotent — a missing folder returns 204.
    """
    await _load_owned_workflow(workflow_id, current_user.id)

    node_dir = _workflow_node_sample_dir(workflow_id, node_id)
    if node_dir.exists():
        try:
            shutil.rmtree(node_dir)
        except OSError as exc:
            logger.warning(
                f"[AGENT] workflow_node_sample: could not remove {node_dir}: {exc}"
            )
    return None
