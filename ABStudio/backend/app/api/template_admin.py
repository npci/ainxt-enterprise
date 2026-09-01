# SPDX-License-Identifier: Apache-2.0
"""
Optional template editor API — feature flagged.

This module is the one and only entrypoint for editing seed templates
from the UI. Everything new lives here under the `/template-admin/`
prefix to avoid colliding with `GET /templates/{template_id}` in
`templates.py`:
  - POST   /template-admin                       create a brand-new template (persisted to workflow_repo.py)
  - PUT    /template-admin/{id}                  update name/description/category/pattern/hitl/graph
  - DELETE /template-admin/{id}                  remove a template row
  - POST   /template-admin/{id}/save-to-seed     rewrite the row's literal entry in `_SEED_TEMPLATES`
  - POST   /template-admin/{id}/reset            restore the row to its `_SEED_TEMPLATES` definition
  - GET    /template-admin/export-snapshot       dump current DB rows for manual sync
  - GET    /template-admin/status                cheap probe — is the editor enabled?

Edits are written to the `templates` table by default; `save-to-seed`
and `create` additionally patch this repo's `app/core/workflow_repo.py`
in place — replacing the matching `_SEED_TEMPLATES` entry by id, or
appending a new one before the closing `]`. That makes the catalog
under version control reflect exactly what the editor produces, and
keeps saved edits across a DB wipe.

Removal recipe
--------------
1. Delete this file.
2. In `app/main.py`, remove the `template_admin` import and its entry in
   the `include_router` list.
3. (Optional) Delete the "Optional template editor support" block in
   `app/core/workflow_repo.py` (look for `_EDITABLE_TEMPLATE_FIELDS`).
4. Unset `TEMPLATES_EDITABLE` in your environment.

That's it — the read path, seed path, and use_template flow have zero
dependencies on this module.

Feature flag
------------
The endpoints are only mounted when the env var `TEMPLATES_EDITABLE` is
truthy (`1`, `true`, `yes`, case-insensitive). When the flag is off,
calls return 404 just like any unknown route.
"""
import json

import os
from fastapi import APIRouter, Depends, HTTPException, Response

from app.models import AuthenticatedUser
from app.core import workflow_repo
from app.api.deps import require_access, require_admin

from core.logger import logger
router = APIRouter()


def _is_enabled() -> bool:
    return os.getenv("TEMPLATES_EDITABLE", "").strip().lower() in {"1", "true", "yes", "on"}


def _require_enabled() -> None:
    if not _is_enabled():
        # Mirror FastAPI's default 404 so the flag-off state is
        # indistinguishable from "no such endpoint" — no info leak.
        raise HTTPException(status_code=404, detail="Not Found")


@router.put("/template-admin/{template_id}")
async def update_template_route(
    template_id: str,
    data: dict,
    current_user: AuthenticatedUser = Depends(require_admin),
):
    """Update a template row. Body accepts any subset of:
    `name`, `description`, `category`, `pattern`, `hitl`, `graphData`.
    Unknown fields are ignored. `id` is not editable."""
    _require_enabled()
    try:
        updated = await workflow_repo.update_template(template_id, data)
    except Exception:
        logger.exception(f'[AGENT] update_template failed for {template_id}')
        raise HTTPException(status_code=500, detail="update failed")
    if not updated:
        raise HTTPException(status_code=404, detail="Template not found")
    return updated


@router.delete("/template-admin/{template_id}", status_code=204)
async def delete_template_route(
    template_id: str,
    current_user: AuthenticatedUser = Depends(require_admin),
):
    """Delete a template row. The next restart will NOT resurrect it —
    use `/templates/{id}/reset` to restore from the seed."""
    _require_enabled()
    try:
        removed = await workflow_repo.delete_template(template_id)
    except Exception:
        logger.exception(f'[AGENT] delete_template failed for {template_id}')
        raise HTTPException(status_code=500, detail="delete failed")
    if not removed:
        raise HTTPException(status_code=404, detail="Template not found")
    return Response(status_code=204)


@router.post("/template-admin/{template_id}/save-to-seed")
async def save_to_seed_route(
    template_id: str,
    current_user: AuthenticatedUser = Depends(require_admin),
):
    """Rewrite the template's literal entry inside `_SEED_TEMPLATES`
    in `workflow_repo.py` so the DB state becomes the new code-level
    baseline. After this call, "Reset to seed" will restore THIS saved
    version and the row will survive a DB wipe."""
    _require_enabled()
    try:
        saved = await workflow_repo.save_template_to_seed(template_id)
    except Exception:
        logger.exception(f'[AGENT] save_template_to_seed failed for {template_id}')
        raise HTTPException(status_code=500, detail="save-to-seed failed")
    if not saved:
        raise HTTPException(status_code=404, detail="Template not found")
    return saved


@router.post("/template-admin", status_code=201)
async def create_template_route(
    data: dict,
    current_user: AuthenticatedUser = Depends(require_admin),
):
    """Create a brand-new template row AND persist it to the seed
    overrides sidecar so it survives a DB wipe. Required fields:
    `id`, `name`, `graphData`. Optional: `description`, `category`,
    `pattern`, `hitl`."""
    _require_enabled()
    try:
        created = await workflow_repo.create_template(data)
    except Exception:
        logger.exception('[AGENT] create_template failed')
        raise HTTPException(status_code=500, detail="create failed")
    if not created:
        raise HTTPException(
            status_code=400,
            detail="invalid payload or template id already exists",
        )
    return created


@router.post("/template-admin/{template_id}/reset")
async def reset_template_route(
    template_id: str,
    current_user: AuthenticatedUser = Depends(require_admin),
):
    """Restore the template row to its `_SEED_TEMPLATES` definition.
    Re-inserts the row if it was deleted; overwrites edits otherwise."""
    _require_enabled()
    try:
        restored = await workflow_repo.reset_template_to_seed(template_id)
    except Exception:
        logger.exception(f'[AGENT] reset_template failed for {template_id}')
        raise HTTPException(status_code=500, detail="reset failed")
    if not restored:
        raise HTTPException(status_code=404, detail="Template not in seed")
    return restored


@router.get("/template-admin/export-snapshot")
async def export_snapshot_route(
    current_user: AuthenticatedUser = Depends(require_admin),
):
    """Return the current DB state of all visible templates so a
    developer can diff against `_SEED_TEMPLATES` and manually merge
    edits back into `workflow_repo.py` if they want a permanent record."""
    _require_enabled()
    rows = await workflow_repo.export_templates_snapshot()
    return {"count": len(rows), "templates": rows}


@router.get("/template-admin/status")
async def status_route():
    """Cheap probe — returns whether the editor is enabled. Public so a
    frontend can decide whether to show 'Edit' buttons without needing
    to attempt a privileged call first."""
    return {"editable": _is_enabled()}
