# SPDX-License-Identifier: Apache-2.0
"""
routers/prompt_mgmt_router.py — P10: Prompt version management API.

Endpoints:
  POST   /prompt-versions                          — register a new version
  GET    /prompt-versions/{key}                    — list versions for a key
  POST   /prompt-versions/{key}/activate/{version} — activate a version
  POST   /prompt-versions/{key}/rollback           — rollback to previous version
  POST   /prompt-versions/{key}/ab-test            — start an A/B test
  GET    /prompt-versions/{key}/eval-scores        — get eval scores per version

All endpoints require admin role.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

from auth.dependencies import get_current_user as _require_auth
from core.logger import logger

router = APIRouter(prefix="/prompt-versions", tags=["prompt-management"])


def _require_admin(current_user: dict = Depends(_require_auth)) -> dict:
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return current_user


class RegisterPromptRequest(BaseModel):
    key:     str
    content: str
    author:  Optional[str] = "system"


class ABTestRequest(BaseModel):
    control_version: int
    variant_version: int
    variant_pct:     float = 10.0


@router.post("", status_code=201)
async def register_prompt(
    body: RegisterPromptRequest,
    current_user: dict = Depends(_require_admin),
):
    """Register a new prompt version (does not activate automatically)."""
    try:
        from core.prompt_registry import prompt_registry
        version = prompt_registry.register(
            key=body.key,
            content=body.content,
            author=body.author or current_user.get("user_id", "system"),
        )
        return {"key": body.key, "version": version, "status": "registered"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to register prompt: {e}")


@router.get("/{key}", status_code=200)
async def list_prompt_versions(
    key: str,
    current_user: dict = Depends(_require_admin),
):
    """List all versions for a prompt key."""
    try:
        from db.database import SessionLocal
        from db.models import PromptVersion

        db = SessionLocal()
        try:
            versions = (
                db.query(PromptVersion)
                .filter(PromptVersion.prompt_key == key)
                .order_by(PromptVersion.version.desc())
                .all()
            )
            return {
                "key": key,
                "versions": [
                    {
                        "version":    v.version,
                        "is_active":  v.is_active,
                        "is_control": v.is_control,
                        "traffic_pct": v.traffic_pct,
                        "eval_score": v.eval_score,
                        "author":     v.author,
                        "created_at": v.created_at.isoformat() if v.created_at else None,
                        "content_preview": (v.content or "")[:100],
                    }
                    for v in versions
                ],
            }
        finally:
            db.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list versions: {e}")


@router.post("/{key}/activate/{version}", status_code=200)
async def activate_prompt_version(
    key: str,
    version: int,
    current_user: dict = Depends(_require_admin),
):
    """Activate a specific version of a prompt."""
    try:
        from core.prompt_registry import prompt_registry
        prompt_registry.activate(key, version)
        return {"key": key, "version": version, "status": "activated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to activate: {e}")


@router.post("/{key}/rollback", status_code=200)
async def rollback_prompt(
    key: str,
    current_user: dict = Depends(_require_admin),
):
    """Rollback to the previous active version."""
    try:
        from core.prompt_registry import prompt_registry
        prev_version = prompt_registry.rollback(key)
        return {"key": key, "rolled_back_to": prev_version, "status": "rolled_back"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {e}")


@router.post("/{key}/ab-test", status_code=200)
async def start_ab_test(
    key: str,
    body: ABTestRequest,
    current_user: dict = Depends(_require_admin),
):
    """Start an A/B test between two versions of a prompt."""
    import os
    if os.getenv("PROMPT_AB_TEST_ENABLED", "false").lower() not in ("1", "true", "yes"):
        raise HTTPException(
            status_code=400,
            detail="A/B testing is disabled. Set PROMPT_AB_TEST_ENABLED=true to enable.",
        )
    try:
        from core.prompt_registry import prompt_registry
        prompt_registry.start_ab_test(
            key=key,
            control_version=body.control_version,
            variant_version=body.variant_version,
            variant_pct=body.variant_pct,
        )
        return {
            "key":             key,
            "control_version": body.control_version,
            "variant_version": body.variant_version,
            "variant_pct":     body.variant_pct,
            "status":          "ab_test_started",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"A/B test start failed: {e}")


@router.get("/{key}/eval-scores", status_code=200)
async def get_eval_scores(
    key: str,
    current_user: dict = Depends(_require_admin),
):
    """Get eval scores for all versions of a prompt key."""
    try:
        from db.database import SessionLocal
        from db.models import PromptVersion

        db = SessionLocal()
        try:
            versions = (
                db.query(PromptVersion)
                .filter(PromptVersion.prompt_key == key)
                .order_by(PromptVersion.version.desc())
                .all()
            )
            return {
                "key": key,
                "eval_scores": [
                    {
                        "version":    v.version,
                        "eval_score": v.eval_score,
                        "is_active":  v.is_active,
                        "is_control": v.is_control,
                    }
                    for v in versions
                ],
            }
        finally:
            db.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get eval scores: {e}")
