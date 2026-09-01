# SPDX-License-Identifier: Apache-2.0
# ============================================================
# MODEL GOVERNANCE ROUTER
# Admin-only endpoints for model access control.
#
# The admin UI (ai-ui/src/components/ModelGovernance.jsx) governs access
# purely per-user: pick a model, then grant/restrict it for individual
# users. There is no department picker in the UI any more — department is
# no longer a governance axis a human manages.
#
# GET  /model-governance/models          — list available model IDs
# GET  /model-governance/my-models       — models allowed for the authenticated user
# GET  /model-governance/users           — list all active users (for the user-picker)
# GET  /model-governance/user-permissions — ALL user-level overrides (no dept filter)
# POST /model-governance/user            — set/update a user-level override
# DELETE /model-governance/user/{user_id}/{model_id} — remove user override
#
# Legacy department-scoped endpoints below (GET/POST "" , GET/DELETE
# /{dept}...) are kept for backward compatibility with any other internal
# caller and because dept_model_permissions rows written before this change
# are still honoured at runtime by filter_allowed_models()/
# is_web_search_allowed() (user-level rules always take precedence over
# them) — but nothing in the current UI creates new department rules any
# more.
# ============================================================

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as _text
from auth.dependencies import get_current_user
from auth.rbac import require_role
from core.security_validation import validate_model_permission_request, _flatten_errors
import logging

router = APIRouter(prefix="/model-governance", tags=["model-governance"])
logger = logging.getLogger(__name__)

# ── Shared dependencies ───────────────────────────────────────────────────────
# _require_admin: enforces that the caller is authenticated AND has role=admin.
# Applied to every endpoint that reads or mutates governance state.
# /my-models is the only exception — it is user-facing (returns the caller's
# own allowed models) and requires only authentication, not admin.
_require_admin = require_role("admin")


def _get_db():
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class ModelPermissionBody(BaseModel):
    department: str
    model_id:   str
    allowed:    bool = True
    web_search_allowed: bool = False


class UserPermissionBody(BaseModel):
    # department is no longer supplied by the UI (governance is user-only
    # now) — kept optional for backward compatibility with any other
    # caller; when omitted the server resolves it from the target user's
    # own users.department so existing department-scoped queries/rows
    # still make sense.
    department: Optional[str] = None
    user_id:    str
    model_id:   str
    allowed:    bool = True
    web_search_allowed: bool = False


# ── Shared governance helpers (also imported by ABStudio/generation.py) ──────

def filter_allowed_models(model_ids, user_id: str, department: str, db) -> List[str]:
    """Return the subset of ``model_ids`` that the user is permitted to use.

    Resolution: user-level override beats dept-level rule; absent = allowed.
    A single ``UNION ALL`` round-trip pulls both rule sets so callers don't
    pay for two sequential SELECTs. Fails open if ``user_model_permissions``
    doesn't exist yet (the more permissive of the two tables to be missing).
    """
    try:
        rows = db.execute(
            _text(
                "SELECT 'u' AS scope, model_id, allowed "
                "FROM user_model_permissions WHERE user_id = :uid "
                "UNION ALL "
                "SELECT 'd' AS scope, model_id, allowed "
                "FROM dept_model_permissions WHERE department = :dept"
            ),
            {"uid": user_id or "", "dept": department or ""},
        ).fetchall()
    except Exception:
        # user_model_permissions may not exist on older schemas — retry with
        # the dept-only query so we still honour department rules.
        try:
            rows = db.execute(
                _text(
                    "SELECT 'd' AS scope, model_id, allowed "
                    "FROM dept_model_permissions WHERE department = :dept"
                ),
                {"dept": department or ""},
            ).fetchall()
        except Exception:
            return list(model_ids)

    dept_rules = {r.model_id: r.allowed for r in rows if r.scope == "d"}
    user_rules = {r.model_id: r.allowed for r in rows if r.scope == "u"}
    return [m for m in model_ids if user_rules.get(m, dept_rules.get(m, True))]


def is_web_search_allowed(model_id: str, user_id: str, department: str, db) -> bool:
    """Resolve effective Web Search permission for a user and model.

    Resolution order mirrors existing overrides:
      1. If the model itself is not allowed, deny.
      2. User-level `web_search_allowed` wins when present.
      3. Else department-level `web_search_allowed` is used.
      4. Missing rule defaults to deny.
    """
    if not model_id:
        return False

    allowed_models = filter_allowed_models([model_id], user_id, department, db)
    if model_id not in allowed_models:
        return False

    try:
        rows = db.execute(
            _text(
                "SELECT 'u' AS scope, model_id, web_search_allowed "
                "FROM user_model_permissions WHERE user_id = :uid AND model_id = :model "
                "UNION ALL "
                "SELECT 'd' AS scope, model_id, web_search_allowed "
                "FROM dept_model_permissions WHERE department = :dept AND model_id = :model"
            ),
            {"uid": user_id or "", "dept": department or "", "model": model_id},
        ).fetchall()
    except Exception as exc:
        from agents.redactor import redact_all
        safe_exc, _ = redact_all(str(exc), {"SECRET", "API_KEY", "ACCESS_TOKEN"})
        logger.warning("Web Search governance lookup failed for model=%s user=%s dept=%s: %s", model_id, user_id, department, safe_exc)
        return False

    user_rule = next((bool(r.web_search_allowed) for r in rows if r.scope == "u"), None)
    if user_rule is not None:
        return user_rule

    dept_rule = next((bool(r.web_search_allowed) for r in rows if r.scope == "d"), None)
    if dept_rule is not None:
        return dept_rule

    return False


# ── All available model IDs (for the UI picker) ──────────────────────────────

def _all_model_ids() -> List[str]:
    from core.model_registry import (
        CLAUDE_PRIMARY_MODEL, CLAUDE_HAIKU,
        CLAUDE_OPUS_MODEL, CLAUDE_OPUS_48_MODEL, CLAUDE_OPUS_5_MODEL,
        CLAUDE_SONNET_5_MODEL,
        OPENAI_CODING_MODEL, OPENAI_LATEST_MODEL, OPENAI_SIMPLE_MODEL,
        OPENAI_TERA_MODEL, OPENAI_LUNA_MODEL,
        ENABLE_GPT56_TERA, ENABLE_GPT56_LUNA,
        GEMINI_TEXT_MODEL, GEMINI_CODING_LITE_MODEL, GEMINI_VISION_MODEL,
        VEO_MODEL, VEO_ENABLED,
        BLOCKED_MODELS,
    )
    # CLAUDE_SONNET_5_MODEL is exposed on ALL channels (web Chat, CLI, IDE) —
    # gated only by the global ENABLE_SONNET_5 kill-switch.
    # Opus 4.6 is retired and always in BLOCKED_MODELS.
    # Opus 4.7/4.8 are CLI/IDE-only; Opus 5 is opt-in — all filtered via BLOCKED_MODELS.
    # Opus models are excluded when ENABLE_OPUS=false (the default) via BLOCKED_MODELS.
    # All three Gemini models are included so they can be governed independently.
    # GPT-5.6 Tera and Luna are included only when their feature flags are enabled.
    # Veo 3.1 is included only when VEO_ENABLED=true so it stays invisible in
    # governance when the feature flag is off.
    base = [
        CLAUDE_PRIMARY_MODEL, CLAUDE_HAIKU,
        CLAUDE_OPUS_MODEL, CLAUDE_OPUS_48_MODEL, CLAUDE_OPUS_5_MODEL,
        CLAUDE_SONNET_5_MODEL,
        OPENAI_CODING_MODEL, OPENAI_LATEST_MODEL, OPENAI_SIMPLE_MODEL,
        GEMINI_TEXT_MODEL, GEMINI_CODING_LITE_MODEL, GEMINI_VISION_MODEL,
        *([OPENAI_TERA_MODEL] if ENABLE_GPT56_TERA else []),
        *([OPENAI_LUNA_MODEL] if ENABLE_GPT56_LUNA else []),
        *([VEO_MODEL] if VEO_ENABLED else []),
    ]
    try:
        from gateway_local_llm import get_local_gateway
        gw = get_local_gateway()
        local = [f"local:{m}" for m in gw.list_models()]
        base = base + local
    except Exception:
        pass
    # Exclude any model that is currently blocked (e.g. Opus when ENABLE_OPUS=false)
    return [m for m in base if m not in BLOCKED_MODELS]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_all_permissions(
        _admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """List all department-level model permissions. Admin only."""
    rows = db.execute(_text(
        "SELECT department, model_id, allowed, web_search_allowed, created_by, created_at "
        "FROM dept_model_permissions ORDER BY department, model_id"
    )).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/models")
def list_available_models(_admin: dict = Depends(_require_admin)):
    """Return all model IDs available for governance assignment. Admin only."""
    return {"models": _all_model_ids()}


# ── IMPORTANT: /my-models MUST be registered before /{dept} ──────────────────
# FastAPI matches routes in registration order. If /{dept} were first, a request
# to /my-models would be captured with dept="my-models" and return wrong data.

@router.get("/my-models")
def get_my_models(
        current_user: dict = Depends(get_current_user),
        db=Depends(_get_db),
):
    """
    Returns the list of model IDs available to the authenticated user.
    Resolution order: dept-level permissions → user-level overrides (user wins).
    Used by Chat UI and IDE extensions (VS Code / JetBrains).
    """
    allowed = filter_allowed_models(
        _all_model_ids(),
        current_user.get("sub", ""),
        current_user.get("department", ""),
        db,
    )
    # governance_loaded=True tells the UI that governance rules were successfully
    # fetched. This lets the frontend distinguish "all models blocked" (empty list
    # + governance_loaded=True) from "governance not yet loaded" (empty list +
    # governance_loaded=False/missing), preventing the fail-open path from
    # showing blocked models when the admin has blocked everything.
    return {"models": allowed, "governance_loaded": True}


# ── User-level override endpoints (department-independent) ───────────────────
# These MUST be registered before /{dept} for the same route-matching reason
# as /my-models above — "users" and "user-permissions" would otherwise be
# captured as a literal department name.

@router.get("/users")
def get_all_users(
        _admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """List all active users for the user-picker. Admin only."""
    rows = db.execute(_text(
        "SELECT id, email, name, role, ad_level, department "
        "FROM users WHERE is_active = TRUE "
        "ORDER BY name"
    )).fetchall()
    return {"users": [dict(r._mapping) for r in rows]}


@router.get("/user-permissions")
def get_all_user_permissions(
        _admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """All user-level model overrides, across every department. Admin only."""
    try:
        rows = db.execute(_text(
            "SELECT user_id, model_id, allowed, web_search_allowed, created_by, created_at "
            "FROM user_model_permissions ORDER BY user_id, model_id"
        )).fetchall()
        return {"permissions": [dict(r._mapping) for r in rows]}
    except Exception:
        return {"permissions": []}


# ── Legacy department-scoped endpoints ────────────────────────────────────────
# Kept for backward compatibility (see module docstring) — no longer called
# by the current UI, which is user-only.

@router.get("/{dept}")
def get_dept_permissions(
        dept: str,
        _admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """List model permissions for a department. Admin only."""
    rows = db.execute(_text(
        "SELECT model_id, allowed, web_search_allowed, created_by, created_at "
        "FROM dept_model_permissions WHERE department = :dept ORDER BY model_id"
    ), {"dept": dept}).fetchall()
    return {"department": dept, "permissions": [dict(r._mapping) for r in rows]}


@router.post("")
def set_model_permission(
        body: ModelPermissionBody,
        admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """Set or update model access for a department. Admin only."""
    ok, errs, san = validate_model_permission_request(body)
    if not ok:
        raise HTTPException(status_code=400, detail=_flatten_errors(errs))
    body.department = san["department"]
    body.model_id = san["model_id"]

    db.execute(_text("""
                     INSERT INTO dept_model_permissions (department, model_id, allowed, web_search_allowed, created_by)
                     VALUES (:dept, :model, :allowed, :web_search_allowed, :created_by)
                         ON CONFLICT (department, model_id)
        DO UPDATE SET allowed = EXCLUDED.allowed,
                      web_search_allowed = EXCLUDED.web_search_allowed,
                      created_by = EXCLUDED.created_by,
                      created_at = NOW()
                     """), {
        "dept": body.department,
        "model": body.model_id,
        "allowed": body.allowed,
        "web_search_allowed": body.web_search_allowed,
        "created_by": admin.get("sub") or admin.get("email", "unknown"),
    })
    db.commit()
    logger.info(
        "governance: dept permission set by admin=%s dept=%s model=%s allowed=%s web_search=%s",
        admin.get("email", "?"), body.department, body.model_id,
        body.allowed, body.web_search_allowed,
    )
    return {
        "ok": True,
        "department": body.department,
        "model_id": body.model_id,
        "allowed": body.allowed,
        "web_search_allowed": body.web_search_allowed,
    }


@router.delete("/{dept}/{model_id}")
def delete_model_permission(
        dept: str,
        model_id: str,
        admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """Remove a department-level model permission. Admin only."""
    db.execute(_text(
        "DELETE FROM dept_model_permissions WHERE department = :dept AND model_id = :model"
    ), {"dept": dept, "model": model_id})
    db.commit()
    logger.info(
        "governance: dept permission deleted by admin=%s dept=%s model=%s",
        admin.get("email", "?"), dept, model_id,
    )
    return {"ok": True}


@router.get("/{dept}/users")
def get_dept_users(
        dept: str,
        _admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """List all active users for user-level model override management. Admin only."""
    rows = db.execute(_text(
        "SELECT id, email, name, role, ad_level, department "
        "FROM users WHERE is_active = TRUE "
        "ORDER BY name"
    )).fetchall()
    return {"department": dept, "users": [dict(r._mapping) for r in rows]}


@router.get("/{dept}/user-permissions")
def get_dept_user_permissions(
        dept: str,
        _admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """All user-level model overrides for a department. Admin only."""
    try:
        rows = db.execute(_text(
            "SELECT user_id, model_id, allowed, web_search_allowed, created_by, created_at "
            "FROM user_model_permissions WHERE department = :dept "
            "ORDER BY user_id, model_id"
        ), {"dept": dept}).fetchall()
        return {"department": dept, "permissions": [dict(r._mapping) for r in rows]}
    except Exception:
        return {"department": dept, "permissions": []}


@router.post("/user")
def set_user_model_permission(
        body: UserPermissionBody,
        admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """Set or update a user-level model override. Admin only.

    ``department`` is no longer supplied by the UI — it's resolved here from
    the target user's own ``users.department`` when omitted, so the column
    (used only for informational grouping / the legacy dept-scoped read
    endpoints) stays consistent with reality rather than trusting whatever
    the caller sends.
    """
    ok, errs, san = validate_model_permission_request(body)
    if not ok:
        raise HTTPException(status_code=400, detail=_flatten_errors(errs))
    body.department = san["department"]
    body.model_id = san["model_id"]
    body.user_id = san.get("user_id", body.user_id)

    dept = body.department
    if not dept:
        row = db.execute(
            _text("SELECT department FROM users WHERE id = :uid"),
            {"uid": body.user_id},
        ).fetchone()
        dept = (row[0] if row else None) or ""

    db.execute(_text("""
                     INSERT INTO user_model_permissions (department, user_id, model_id, allowed, web_search_allowed, created_by)
                     VALUES (:dept, :user_id, :model, :allowed, :web_search_allowed, :created_by)
                         ON CONFLICT (user_id, model_id)
        DO UPDATE SET allowed = EXCLUDED.allowed,
                      web_search_allowed = EXCLUDED.web_search_allowed,
                      created_by = EXCLUDED.created_by,
                      created_at = NOW()
                     """), {
        "dept": dept,
        "user_id": body.user_id,
        "model": body.model_id,
        "allowed": body.allowed,
        "web_search_allowed": body.web_search_allowed,
        "created_by": admin.get("sub") or admin.get("email", "unknown"),
    })
    db.commit()
    logger.info(
        "governance: user permission set by admin=%s user=%s model=%s allowed=%s web_search=%s",
        admin.get("email", "?"), body.user_id, body.model_id,
        body.allowed, body.web_search_allowed,
    )
    return {
        "ok": True,
        "user_id": body.user_id,
        "model_id": body.model_id,
        "allowed": body.allowed,
        "web_search_allowed": body.web_search_allowed,
    }


@router.delete("/user/{user_id}/{model_id}")
def delete_user_model_permission(
        user_id: str,
        model_id: str,
        admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """Remove a user-level model override (user reverts to dept-level rule). Admin only."""
    try:
        db.execute(_text(
            "DELETE FROM user_model_permissions WHERE user_id = :uid AND model_id = :model"
        ), {"uid": user_id, "model": model_id})
        db.commit()
        logger.info(
            "governance: user permission deleted by admin=%s user=%s model=%s",
            admin.get("email", "?"), user_id, model_id,
        )
    except Exception:
        pass
    return {"ok": True}


# ── Runtime enforcement helper (called by model_router.py) ───────────────────

def is_model_allowed_for_dept(model_id: str, department: Optional[str]) -> bool:
    """
    Check if model_id is permitted for the given department.
    Returns True if no restriction exists (open by default).
    Called at request time from model_router.py.
    """
    if not department:
        return True
    try:
        from db.database import engine
        with engine.connect() as conn:
            row = conn.execute(_text("""
                                     SELECT allowed FROM dept_model_permissions
                                     WHERE department = :dept AND model_id = :model
                                         LIMIT 1
                                     """), {"dept": department, "model": model_id}).fetchone()
        if row is None:
            return True   # no rule = allowed
        return bool(row[0])
    except Exception:
        return True   # fail-open: don't block on DB error
