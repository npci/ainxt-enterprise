# SPDX-License-Identifier: Apache-2.0
"""
Cowork admin/setup router — REST surface for:
  - per-user personalization prefs (self-service)  → memory/cowork_memory.py
  - role specialist packs (admin-managed)          → services/cowork_roles.py

Prefs are scoped to the caller (JWT sub). Role writes require admin; role reads
are department/visibility-scoped by the service.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import get_current_user
from auth.rbac import require_admin
from core.config import BUDDY_QUEUE_MAX_WAIT
from core.security_validation import (
    validate_cowork_note_request,
    validate_cowork_prefs_request,
    validate_cowork_role_request,
    _flatten_errors,
)

from core.logger import logger, mask_email

router = APIRouter(prefix="/buddy", tags=["buddy"])


# ── Preferences (self-service) ────────────────────────────────────────────────
class PrefsUpdate(BaseModel):
    prefs: Dict[str, Any]


@router.get("/prefs")
async def get_my_prefs(current_user: dict = Depends(get_current_user)):
    from memory.cowork_memory import get_prefs
    return {"prefs": get_prefs(current_user["sub"])}


@router.get("/model-config")
async def get_model_config(current_user: dict = Depends(get_current_user)):
    """Buddy model selection policy — OPS-CONFIGURABLE via gateway env vars so the
    default/locked model can change per deployment without a UI rebuild.

    Env:
      BUDDY_FORCED_MODEL   default model id the desktop pins to (default Opus 4.8)
      BUDDY_MODEL_LOCKED   "true" = hide the picker + disable switching (default true)
    The desktop reads this at startup; when locked=false the picker returns.
    """
    import os
    forced = os.getenv("BUDDY_FORCED_MODEL", "").strip()
    locked = os.getenv("BUDDY_MODEL_LOCKED", "true").strip().lower() in ("1", "true", "yes")
    return {"forced_model": forced, "locked": locked}


@router.get("/queue-config")
async def get_queue_config(current_user: dict = Depends(get_current_user)):
    """Buddy prompt queue configuration — read by the frontend on mount (GET /buddy/queue-config).

    Returns the maximum number of prompts a user can queue in Buddy while one
    is processing. Controlled by the BUDDY_QUEUE_MAX_WAIT env var (default 5).
    Set to 0 for unlimited. Change by updating .env and restarting the gateway.
    """
    return {"max_wait": BUDDY_QUEUE_MAX_WAIT}


@router.get("/memory/prompt")
async def get_my_memory_prompt(current_user: dict = Depends(get_current_user)):
    """Render the caller's durable Cowork memory (prefs + agent-saved notes) as a
    system-prompt snippet. The desktop Cowork agent fetches this at session start
    and appends it, so remembered facts carry across tasks. Empty string = none."""
    from memory.cowork_memory import build_memory_prompt
    return {"prompt": build_memory_prompt(current_user["sub"]) or ""}


@router.put("/prefs")
async def set_my_prefs(body: PrefsUpdate, current_user: dict = Depends(get_current_user)):
    from memory.cowork_memory import set_pref, get_prefs
    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # free-text-ish keys get an XSS check before being persisted.
    is_valid, field_errors, sanitized = validate_cowork_prefs_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.prefs = sanitized["prefs"]

    # Only persist known, simple keys; ignore unknowns silently.
    allowed = {"email_signature", "default_doc_format", "preferred_ppt_theme", "tone",
               "team_aliases", "channel_aliases", "role"}
    uid = current_user["sub"]
    try:
        for k, v in (body.prefs or {}).items():
            if k in allowed:
                set_pref(uid, k, v)
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    return {"prefs": get_prefs(uid)}


# ── Durable memory notes (self-service: view / add / forget) ──────────────────
class NoteBody(BaseModel):
    note: str


@router.post("/memory/note")
async def add_my_note(body: NoteBody, current_user: dict = Depends(get_current_user)):
    """Let the user add a durable fact themselves (parity with the agent's
    `remember` tool). Compliance-gated identically — a note carrying a
    secret/PAN/PII is refused, never stored, so the prompt store stays clean."""
    note = (body.note or "").strip()
    if not note:
        raise HTTPException(400, detail="note is required")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_cowork_note_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    note = sanitized["note"]
    try:
        from mcp.servers.base import _compliance_check
        blk = _compliance_check(note)
        if blk:
            raise HTTPException(422, detail=f"Can't remember sensitive content ({blk}).")
    except HTTPException:
        raise
    except Exception as exc:  # compliance unavailable → fail closed for safety
        logger.error(f"cowork memory note compliance check failed: {exc}")
        raise HTTPException(503, detail="memory unavailable")
    from memory.cowork_memory import add_note, get_prefs
    add_note(current_user["sub"], note)
    return {"prefs": get_prefs(current_user["sub"])}


@router.delete("/memory/note")
async def delete_my_note(note: str, current_user: dict = Depends(get_current_user)):
    """Forget one durable fact (exact text). Idempotent — unknown note = no-op."""
    from memory.cowork_memory import delete_note
    return {"prefs": delete_note(current_user["sub"], (note or "").strip())}


# ── Role specialists ──────────────────────────────────────────────────────────
class RoleBody(BaseModel):
    name: str
    system_prompt: str
    description: str = ""
    allowed_connectors: List[str] = []
    skill_names: List[str] = []
    subagent_allowlist: List[str] = []
    department: str = ""
    visibility: str = "private"


@router.get("/roles")
async def list_cowork_roles(published: bool = False, current_user: dict = Depends(get_current_user)):
    """List role specialists visible to the caller's department.
    - `published=true` → ONLY published roles (the org marketplace). This is the
      governance gate: the end-user role PICKER must use this so unpublished DRAFTS
      never appear to users. Publishing (admin) is what makes a role org-visible.
    - default → all visible roles incl. drafts, for the admin management UI."""
    from services.cowork_roles import list_for_picker, list_owned, list_all_roles, list_pending
    from auth.rbac import can_approve as _can_approve
    is_admin = (current_user.get("role") == "admin")
    uid = current_user["sub"]
    dept = current_user.get("department", "") or ""
    if published:
        # PICKER scope (3-tier, governance-gated): APPROVED public ∪ APPROVED dept-private ∪ your own.
        return {"roles": [r.to_dict() for r in list_for_picker(uid, dept)]}
    # MANAGEMENT scope: admins see all; approvers also see the pending queue; users see their own.
    if is_admin:
        rows = list_all_roles()
    elif _can_approve(current_user):
        seen = {r.id: r for r in list_owned(uid)}
        for r in list_pending():
            seen[r.id] = r
        rows = list(seen.values())
    else:
        rows = list_owned(uid)
    return {"roles": [r.to_dict() for r in rows]}


@router.get("/roles/{role_id}/context")
async def get_role_context(role_id: str, current_user: dict = Depends(get_current_user)):
    """Full operating context for a role specialist: its specialist prompt PLUS its
    bundled behavioral-skill SOPs, rendered server-side (DB is source of truth). The
    desktop Cowork session fetches this at start and injects it as the agent's [ROLE]
    context — so a role actually bundles Skills, not just a prompt. Empty = unknown role."""
    from services.cowork_roles import build_role_context
    dept = current_user.get("department", "") or ""
    return {"prompt": build_role_context(role_id, department=dept) or ""}


def _is_admin(u: dict) -> bool:
    return (u.get("role") == "admin")


def _owns_or_admin(role_id: str, user: dict):
    """Return the role if the caller owns it or is an admin, else raise 403/404."""
    from services.cowork_roles import get_role
    role = get_role(role_id)
    if not role:
        raise HTTPException(404, detail="Role not found")
    if not _is_admin(user) and (role.created_by or "") != user["sub"]:
        raise HTTPException(403, detail="You can only edit your own roles.")
    return role


def _notify_role_approvers(role, actor: str) -> None:
    """Inbox-notify the recipients who should approve this Cowork role.

    Routing (mirrors budget-approval routing — auth.rbac.resolve_request_approvers):
      - the creator's own HOD (users.hod_email), plus any delegatees that HOD
        has nominated (department_hod_mapping.delegated_to).
      - falls back to every active admin/ad_level<=3 user when the creator
        has no resolvable HOD, so a submission is never left unrouted.
    Exactly one inbox row is written per recipient.
    """
    try:
        from db.database import SessionLocal
        from db.models import User
        from sqlalchemy import or_, func
        from store.inbox_store import publish_inbox_item
        from auth.rbac import resolve_request_approvers

        db = SessionLocal()
        try:
            creator = db.query(User).filter(User.id == role.created_by).first() if role.created_by else None
            creator_email = (creator.email if creator else "") or ""

            approvers = resolve_request_approvers(creator_email)
            hod_email = approvers.get("hod_email")
            delegatee_emails = approvers.get("delegatee_emails") or []
            recipient_emails = ([hod_email] if hod_email else []) + delegatee_emails

            if recipient_emails:
                recipients = db.query(User).filter(
                    func.lower(User.email).in_([e.lower() for e in recipient_emails]),
                    User.is_active == True).all()
            else:
                # Fallback: no resolvable HOD — notify configurable approval level
                _approval_level = int(os.getenv("APPROVAL_AD_LEVEL", "3"))
                recipients = db.query(User).filter(
                    or_(User.ad_level <= _approval_level, User.role == "admin"),
                    User.is_active == True).all()
            recipient_ids = [str(u.id) for u in recipients]
        finally:
            db.close()
        scope = "organization-wide" if role.visibility == "public" else f"the '{role.department or 'department'}' department"
        for uid in recipient_ids:
            publish_inbox_item(
                uid, "approval", f"Cowork role pending approval: {role.name}",
                f"**{actor}** submitted the Cowork role **{role.name}** for {scope} use. "
                f"Review and Approve/Reject it in Cowork Setup.",
                source_id=role.id, metadata={"kind": "cowork_role", "role_id": role.id,
                                              "visibility": role.visibility,
                                              "created_by": role.created_by,
                                              "hod_email": hod_email,
                                              "delegatee_emails": delegatee_emails},
            )
    except Exception as e:
        logger.warning(f"cowork_roles: approver notify failed: {e}")


def _notify_role_owner(role, outcome: str, actor: str) -> None:
    """Inbox-notify the role's creator that their submission was approved/rejected."""
    try:
        from store.inbox_store import publish_inbox_item
        if not role.created_by:
            return
        publish_inbox_item(
            str(role.created_by), "approval", f"Cowork role {outcome}: {role.name}",
            f"Your Cowork role **{role.name}** was **{outcome}** by `{actor}`.",
            source_id=role.id, metadata={"kind": "cowork_role", "role_id": role.id, "outcome": outcome,
                                          "visibility": role.visibility, "created_by": role.created_by},
        )
    except Exception as e:
        logger.warning(f"cowork_roles: owner notify failed: {e}")


@router.post("/roles", status_code=201)
async def create_cowork_role(body: RoleBody, current_user: dict = Depends(get_current_user)):
    """Any user can create a role specialist. GOVERNANCE (mirrors the KB):
      - personal (just me) → APPROVED immediately, no review.
      - private (department) / public (org-wide) → PENDING_APPROVAL; an approver
        (ad_level ≤ 3 / admin) must approve before it's visible. Auto-approved if the
        creator is themselves an approver (like KB). 'public' is reached by submitting
        a public-visibility role for approval (admin form), not a Publish toggle."""
    from services.cowork_roles import CoworkRole, create_role, set_role_status, get_role
    from auth.rbac import can_approve as _can_approve

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_cowork_role_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.name = sanitized["name"]
    body.system_prompt = sanitized["system_prompt"]
    body.description = sanitized["description"]
    body.allowed_connectors = sanitized["allowed_connectors"]
    body.skill_names = sanitized["skill_names"]
    body.subagent_allowlist = sanitized["subagent_allowlist"]

    # Tiers a user may request: personal / private (own dept). Admins may also request public.
    allowed_vis = ("personal", "private", "public") if _is_admin(current_user) else ("personal", "private")
    visibility = body.visibility if body.visibility in allowed_vis else "personal"
    dept = (current_user.get("department") or "").strip()
    actor = current_user.get("email") or current_user["sub"]
    try:
        role = create_role(CoworkRole(
            name=body.name.strip(),
            system_prompt=body.system_prompt,
            description=body.description,
            allowed_connectors=body.allowed_connectors,
            skill_names=body.skill_names,
            subagent_allowlist=body.subagent_allowlist,
            department=dept,
            visibility=visibility,
            created_by=current_user["sub"],
        ))
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    # Governance status
    if visibility == "personal" or _can_approve(current_user):
        set_role_status(role.id, "APPROVED", actor)            # no review / approver self-approves
    else:
        set_role_status(role.id, "PENDING_APPROVAL")
        _notify_role_approvers(get_role(role.id) or role, actor)
    logger.info(f"cowork_roles: created '{body.name}' ({visibility}) by {mask_email(current_user.get('email'))}")
    return (get_role(role.id) or role).to_dict()


@router.put("/roles/{role_id}")
async def update_cowork_role(role_id: str, body: RoleBody, current_user: dict = Depends(get_current_user)):
    from services.cowork_roles import update_role, set_role_status, get_role
    from auth.rbac import can_approve as _can_approve
    role = _owns_or_admin(role_id, current_user)  # owner or admin only

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_cowork_role_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))

    fields = dict(
        name=sanitized["name"].strip(),
        system_prompt=sanitized["system_prompt"],
        description=sanitized["description"],
        allowed_connectors=sanitized["allowed_connectors"],
        skill_names=sanitized["skill_names"],
        subagent_allowlist=sanitized["subagent_allowlist"],
    )
    # department is preserved (the builder doesn't expose it). Visibility tiers a user
    # may set: personal/private; admins may also set public.
    allowed_vis = ("personal", "private", "public") if _is_admin(current_user) else ("personal", "private")
    new_vis = body.visibility if body.visibility in allowed_vis else (role.visibility or "personal")
    fields["visibility"] = new_vis
    updated = update_role(role_id, **fields)
    if not updated:
        raise HTTPException(404, detail="Role not found")
    # Governance re-evaluation (KB-style): personal never needs review; an approver's
    # edit stays APPROVED; a NON-approver editing a shared (private/public) role must
    # re-submit — the content changed, so the prior approval no longer holds.
    actor = current_user.get("email") or current_user["sub"]
    if new_vis == "personal" or _can_approve(current_user):
        set_role_status(role_id, "APPROVED", actor)
    else:
        set_role_status(role_id, "PENDING_APPROVAL")
        _notify_role_approvers(get_role(role_id), actor)
    return (get_role(role_id) or updated).to_dict()


@router.delete("/roles/{role_id}")
async def delete_cowork_role(role_id: str, current_user: dict = Depends(get_current_user)):
    from services.cowork_roles import delete_role
    _owns_or_admin(role_id, current_user)  # owner or admin only
    if not delete_role(role_id):
        raise HTTPException(404, detail="Role not found")
    return {"deleted": True}


def _creator_email(role) -> str:
    """Resolve a CoworkRole.created_by (user id) to an email, empty on any miss."""
    if not role.created_by:
        return ""
    try:
        from db.database import SessionLocal
        from db.models import User
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.id == role.created_by).first()
            return (u.email or "") if u else ""
        finally:
            db.close()
    except Exception:
        return ""


# ── Governance: approve / reject a submitted role ─────────────────────────────
# Authorised: admin, OR the creator's own HOD / one of the HOD's nominated
# delegatees, OR (fallback, when the creator has no resolvable HOD) any
# ad_level<=3 senior approver — mirrors the KB doc-approval gate.
@router.post("/roles/{role_id}/approve")
async def approve_cowork_role(role_id: str, current_user: dict = Depends(get_current_user)):
    """Approve a PENDING role → it becomes visible at its tier (private=dept, public=org)."""
    from auth.rbac import can_approve as _can_approve, is_admin as _is_admin, is_request_approver as _is_request_approver
    from services.cowork_roles import get_role, set_role_status
    role = get_role(role_id)
    if not role:
        raise HTTPException(404, detail="Role not found")
    if not (_is_admin(current_user)
            or _is_request_approver(current_user, _creator_email(role))
            or _can_approve(current_user)):
        raise HTTPException(403, detail="Only the creator's HOD (or their delegate) or an admin can approve roles.")
    actor = current_user.get("email") or current_user["sub"]
    set_role_status(role_id, "APPROVED", actor)
    _notify_role_owner(role, "approved", actor)
    logger.info(f"cowork_roles: '{role.name}' APPROVED by {actor}")
    return (get_role(role_id) or role).to_dict()


@router.post("/roles/{role_id}/reject")
async def reject_cowork_role(role_id: str, current_user: dict = Depends(get_current_user)):
    """Reject a PENDING role → status REJECTED (creator notified; can edit + resubmit)."""
    from auth.rbac import can_approve as _can_approve, is_admin as _is_admin, is_request_approver as _is_request_approver
    from services.cowork_roles import get_role, set_role_status
    role = get_role(role_id)
    if not role:
        raise HTTPException(404, detail="Role not found")
    if not (_is_admin(current_user)
            or _is_request_approver(current_user, _creator_email(role))
            or _can_approve(current_user)):
        raise HTTPException(403, detail="Only the creator's HOD (or their delegate) or an admin can reject roles.")
    actor = current_user.get("email") or current_user["sub"]
    set_role_status(role_id, "REJECTED", actor)
    _notify_role_owner(role, "rejected", actor)
    logger.info(f"cowork_roles: '{role.name}' REJECTED by {actor}")
    return (get_role(role_id) or role).to_dict()


# ── Marketplace publishing (private org marketplace) ──────────────────────────
@router.get("/marketplace")
async def cowork_marketplace(current_user: dict = Depends(get_current_user)):
    """Roles/plugins available to the caller: everything PUBLISHED org-wide plus
    the caller's own department drafts. This is the marketplace browse surface."""
    from services.cowork_roles import list_marketplace
    dept = current_user.get("department", "") or ""
    return {"roles": [r.to_dict() for r in list_marketplace(department=dept)]}


@router.post("/roles/{role_id}/publish")
async def publish_cowork_role(role_id: str, current_user: dict = Depends(require_admin)):
    """Publish a role/plugin to the org marketplace (admin governance gate)."""
    from services.cowork_roles import publish_role
    role = publish_role(role_id, published_by=current_user.get("email") or current_user["sub"])
    if not role:
        raise HTTPException(404, detail="Role not found")
    logger.info(f"cowork_roles: '{role.name}' published by {mask_email(current_user.get('email'))}")
    return role.to_dict()


@router.post("/roles/{role_id}/unpublish")
async def unpublish_cowork_role(role_id: str, current_user: dict = Depends(require_admin)):
    """Withdraw a role/plugin from the marketplace (back to private DRAFT)."""
    from services.cowork_roles import unpublish_role
    role = unpublish_role(role_id)
    if not role:
        raise HTTPException(404, detail="Role not found")
    return role.to_dict()
