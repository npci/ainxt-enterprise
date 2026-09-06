# SPDX-License-Identifier: MIT
"""Governance client — bridges ABStudio artifacts into the platform's
existing governance / approval system.

ABStudio stores the *operational* copy of each artifact (the graph / config
used to run it) in its own tables (``workflows`` / ``agents`` /
``skills_catalog``). The platform-wide governance lifecycle, however, is keyed
off the mirror records in ``agents_pg`` / ``skills_pg`` / ``workflows_pg``
(``db.models``) which the mature ``routers/governance_router`` reads and the
sidebar Inbox renders.

This module mirrors an ABStudio artifact into the matching ``*_pg`` record and
drives the existing governance lifecycle (submit → PENDING_APPROVAL, approve,
etc.) WITHOUT ABStudio having to re-implement any of it. Every call is
best-effort and MUST NOT break artifact create/update if governance wiring is
unavailable (e.g. running ABStudio standalone with no platform DB).

Entity-type strings match the governance router: ``agents``, ``skills``,
``workflows``.
"""
from __future__ import annotations

import hashlib
import json

from typing import Any, Optional

from core.logger import logger
# Statuses that mean "usable" — anything else is blocked at run time.
USABLE_STATUSES = {"APPROVED", "PRODUCTION", "ACTIVE"}

# Map governance entity_type -> (module, class) of the mirror record.
_MODEL_MAP = {
    "agents":    ("db.models", "AgentRecord"),
    "skills":    ("db.models", "SkillRecord"),
    "workflows": ("db.models", "WorkflowRecord"),
}

# Keys stripped before hashing so cosmetic/volatile changes (node positions, ids,
# timestamps, React Flow runtime state) don't count as a "modification" needing
# re-approval. ``measured`` is RF v12's {width,height} written onto every node on
# canvas mount; leaving it in the hash demoted untouched template instances from
# Live to Submit-for-Approval.
_VOLATILE_KEYS = {
    "id", "position", "positionAbsolute", "created_at", "updated_at",
    "createdAt", "updatedAt", "selected", "dragging", "width", "height",
    "x", "y", "zIndex", "__rf",
    "measured", "handleBounds", "internals",
}


# ---------------------------------------------------------------------------
# Canonical hashing (template-modification detection)
# ---------------------------------------------------------------------------

def _strip_volatile(obj: Any) -> Any:
    """Recursively drop volatile keys so the hash reflects only semantic content."""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in _VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def canonical_hash(payload: Any) -> str:
    """Stable SHA-256 of an artifact's semantic content.

    Used to detect whether a template instance has been modified from its
    source template. Two artifacts with identical semantic content hash equal
    regardless of node positions, ids or timestamps.
    """
    try:
        cleaned = _strip_volatile(payload or {})
        blob = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        blob = str(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_user_department(user_id: str) -> str:
    """Look up a user's department from their JWT sub / id (best-effort).

    ABStudio's create/update repo functions only carry ``owner_user_id``; the
    department needed for HOD routing is resolved here from the users table.
    """
    if not user_id:
        return ""
    try:
        from db.database import SessionLocal
        from db.models import User
        db = SessionLocal()
        try:
            u = None
            try:
                u = db.query(User).filter(User.id == int(user_id)).first()
            except (ValueError, TypeError):
                u = db.query(User).filter(User.email == user_id).first()
            return (u.department or "") if u else ""
        finally:
            db.close()
    except Exception as e:
        logger.debug(f'[AGENT] resolve_user_department({user_id}) failed: {e}')
        return ""


# ---------------------------------------------------------------------------
# Status read
# ---------------------------------------------------------------------------

def get_governance_status(entity_type: str, name: str, *, owner_id: str = "") -> Optional[str]:
    """Return the governance status for an artifact, or None if not tracked.

    Delegates to the platform governance router's ``_get_entity_status`` so the
    live source of truth (Redis → in-memory → Postgres, in that priority) is
    honoured. Reading Postgres directly here would risk a stale status because
    approve/promote write Redis first and sync Postgres best-effort. Falls back
    to a direct Postgres read only if the router isn't importable (e.g. ABStudio
    running standalone). None = no governance record exists.

    ``owner_id`` scopes the lookup to the artifact's creator so that two users
    with same-named artifacts on a shared database get independent governance
    statuses. When empty (platform-level artifacts), name-only matching is used.
    """
    if not name:
        return None
    try:
        from routers.governance_router import _get_entity_status
        return _get_entity_status(entity_type, name, owner_id=owner_id)
    except Exception:
        pass
    # Fallback: standalone ABStudio without the platform router mounted.
    mapping = _MODEL_MAP.get(entity_type)
    if not mapping:
        return None
    try:
        import importlib
        from db.database import SessionLocal
        mod = importlib.import_module(mapping[0])
        Model = getattr(mod, mapping[1])
        db = SessionLocal()
        try:
            q = db.query(Model).filter(Model.name == name)
            if owner_id:
                q = q.filter(Model.created_by == owner_id)
            row = q.first()
            return getattr(row, "status", None) if row else None
        finally:
            db.close()
    except Exception as e:
        logger.debug(f'[AGENT] get_governance_status({entity_type}/{name}) failed: {e}')
        return None


def is_usable(entity_type: str, name: str, *, owner_id: str = "") -> bool:
    """True if the artifact may be run.

    A missing governance record is treated as NOT usable for governed entity
    types (fail-closed) so a freshly-created-but-unsubmitted artifact cannot be
    run until it goes through approval. Callers that want to allow ungoverned
    artifacts (e.g. legacy rows created before this feature) should special-case
    a None status.
    """
    status = get_governance_status(entity_type, name, owner_id=owner_id)
    if status is None:
        return False
    return status in USABLE_STATUSES


def bulk_governance_meta(entity_type: str, names: list) -> dict:
    """Bulk-load governance metadata for a list of artifact names.

    Returns ``{name: {status, visibility, department, created_by,
    created_by_email, created_by_name, approved_by, approved_by_email,
    approved_by_name, approved_at, created_at}}``. Best-effort — any DB
    failure yields an empty dict so ABStudio-standalone (no platform DB)
    keeps working. Only used by the Skills tab listing today; kept
    entity-type agnostic so agents/workflows can reuse it later.
    """
    if not names:
        return {}
    mapping = _MODEL_MAP.get(entity_type)
    if not mapping:
        return {}
    try:
        import importlib
        from db.database import SessionLocal
        from db.models import User
        mod = importlib.import_module(mapping[0])
        Model = getattr(mod, mapping[1])
        db = SessionLocal()
        try:
            rows = db.query(Model).filter(Model.name.in_(list(names))).all()
            # Governance mirrors historically store `created_by` as a UUID and
            # `approved_by` as either a UUID or a raw email string, so we can't
            # feed both through User.id.in_(...) (the email breaks the UUID
            # cast and fails the whole query). Split the lookup: UUID-shaped
            # values go to id.in_(), the rest to email.in_().
            import re
            _UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
            uid_set: set = set()
            email_set: set = set()
            for r in rows:
                for attr in ("created_by", "approved_by"):
                    v = getattr(r, attr, None)
                    if not v:
                        continue
                    s = str(v)
                    if _UUID_RE.match(s):
                        uid_set.add(s)
                    elif "@" in s:
                        email_set.add(s.lower())
            users_by_id: dict = {}
            users_by_email: dict = {}
            if uid_set:
                for u in db.query(User).filter(User.id.in_(list(uid_set))).all():
                    entry = {"id": str(u.id), "email": u.email or "", "name": u.name or ""}
                    users_by_id[entry["id"]] = entry
                    if entry["email"]:
                        users_by_email[entry["email"].lower()] = entry
            if email_set:
                for u in db.query(User).filter(User.email.in_(list(email_set))).all():
                    entry = {"id": str(u.id), "email": u.email or "", "name": u.name or ""}
                    users_by_id[entry["id"]] = entry
                    if entry["email"]:
                        users_by_email[entry["email"].lower()] = entry

            def _resolve(v: str) -> dict:
                if not v:
                    return {}
                if _UUID_RE.match(v):
                    return users_by_id.get(v, {})
                return users_by_email.get(v.lower(), {})

            out = {}
            for r in rows:
                cb = str(getattr(r, "created_by", "") or "")
                ab = str(getattr(r, "approved_by", "") or "")
                cb_u = _resolve(cb)
                ab_u = _resolve(ab)
                out[r.name] = {
                    "status":            getattr(r, "status", None),
                    "visibility":        getattr(r, "visibility", None),
                    "department":        getattr(r, "department", None),
                    "created_by":        cb_u.get("id") or (cb or None),
                    "created_by_email":  cb_u.get("email") or (cb if "@" in cb else None),
                    "created_by_name":   cb_u.get("name") or None,
                    "approved_by":       ab_u.get("id") or (ab or None),
                    "approved_by_email": ab_u.get("email") or (ab if "@" in ab else None),
                    "approved_by_name":  ab_u.get("name") or None,
                    "approved_at":       getattr(r, "approved_at", None).isoformat()
                                         if getattr(r, "approved_at", None) else None,
                }
            return out
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"bulk_governance_meta({entity_type}) failed: {e}")
        return {}


# ---------------------------------------------------------------------------
# Mirror-record upsert
# ---------------------------------------------------------------------------

def _upsert_record(
    entity_type: str,
    name: str,
    *,
    status: str,
    created_by: str,
    department: str,
    description: str = "",
    source_template_id: Optional[str] = None,
    source_template_hash: Optional[str] = None,
    last_approved_hash: Optional[str] = None,
    approved_by: Optional[str] = None,
    visibility: Optional[str] = None,
    strict: bool = False,
) -> Optional[str]:
    """Insert or update the ``*_pg`` governance mirror record. Returns prior status.

    ``strict=True`` re-raises any failure instead of swallowing it. Use it on the
    user-facing submit path so a missing migration / unreachable DB surfaces as a
    real error rather than silently leaving no mirror row (which later makes the
    Inbox approve action 404 with "workflow doesn't exist"). Best-effort callers
    (template register / reconcile) keep the default ``strict=False``.
    """
    mapping = _MODEL_MAP.get(entity_type)
    if not mapping:
        if strict:
            raise ValueError(f"Unknown governance entity type: {entity_type}")
        return None
    try:
        import importlib
        from datetime import datetime, timezone
        from db.database import SessionLocal
        mod = importlib.import_module(mapping[0])
        Model = getattr(mod, mapping[1])
        db = SessionLocal()
        try:
            q = db.query(Model).filter(Model.name == name)
            if created_by:
                q = q.filter(Model.created_by == created_by)
            row = q.first()
            prior_status = getattr(row, "status", None) if row else None
            is_prod = status in USABLE_STATUSES
            if row is None:
                row = Model(name=name)
                db.add(row)
            # Only set attributes the model actually has (schemas differ slightly
            # across agents/skills/workflows).
            _set = {
                "status": status,
                "created_by": created_by or getattr(row, "created_by", None),
                "department": department or getattr(row, "department", None),
                # Requested visibility for the published template. Falls back to
                # the existing value, then 'private'.
                "visibility": visibility or getattr(row, "visibility", None) or "private",
                "is_production": is_prod,
                "description": description or getattr(row, "description", None),
                "source_template_id": source_template_id,
                "source_template_hash": source_template_hash,
                "last_approved_hash": last_approved_hash,
            }
            if approved_by is not None:
                _set["approved_by"] = approved_by
                _set["approved_at"] = datetime.now(timezone.utc)
            for k, v in _set.items():
                if v is not None and hasattr(row, k):
                    setattr(row, k, v)
            db.commit()
            return prior_status
        finally:
            db.close()
    except Exception as e:
        logger.warning(f'[AGENT] governance mirror upsert failed for {entity_type}/{name}: {e}')
        if strict:
            raise
        return None


# ---------------------------------------------------------------------------
# Submit for approval
# ---------------------------------------------------------------------------

def submit_for_governance(
    entity_type: str,
    name: str,
    *,
    created_by: str = "",
    actor: str = "",
    department: str = "",
    description: str = "",
    reason: str = "",
    visibility: Optional[str] = None,
    source_template_id: Optional[str] = None,
    source_template_hash: Optional[str] = None,
) -> str:
    """Mirror an ABStudio artifact and submit it for department-HOD approval.

    ``created_by`` is the stored owner id (used for owner-lookup); ``actor`` is
    the human-readable submitter (email/name) shown in notifications. ``reason``
    is the optional submitter note surfaced to the approver. Idempotent: if the
    artifact is already PENDING_APPROVAL it does NOT re-notify (prevents a repeat
    submit from spamming approvers). Returns the resulting status.
    """
    # strict=True: if the mirror row can't be written (e.g. the governance
    # columns migration hasn't been run, or the platform DB is unreachable) this
    # raises instead of silently returning. That prevents the classic failure
    # where submit "succeeds", drops an Inbox item, but no *_pg row exists — so a
    # later approve 404s with "workflow doesn't exist". The API layer turns this
    # into a real 5xx the submitter can see. The returned prior status (read
    # inside the write txn) gates re-notification below — no separate lookup.
    prior = _upsert_record(
        entity_type, name,
        status="PENDING_APPROVAL",
        created_by=created_by,
        department=department,
        description=description,
        visibility=visibility,
        source_template_id=source_template_id,
        source_template_hash=source_template_hash,
        strict=True,
    )
    # Post-write guard: confirm the artifact's governance status now resolves
    # before we notify approvers. Catches a write that landed under a different
    # key/schema than the approve path reads (which would otherwise leave an
    # orphan Inbox item that 404s on approve).
    if get_governance_status(entity_type, name, owner_id=created_by) is None:
        raise RuntimeError(
            f"governance mirror row for {entity_type}/{name} was not persisted "
            f"after submit — check that db/migrate.py has been run on this "
            f"environment and the platform DB is reachable"
        )
    # Only fire notifications/audit on an actual state change into approval.
    # Re-submitting something already pending is a no-op for notifications.
    if prior != "PENDING_APPROVAL":
        _notify_and_record(entity_type, name, "submit", prior or "DRAFT",
                           "PENDING_APPROVAL",
                           actor=actor or created_by or "unknown",
                           reason=reason or None,
                           owner_id=created_by)
    return "PENDING_APPROVAL"


def withdraw_governance(
    entity_type: str,
    name: str,
    *,
    created_by: str = "",
    actor: str = "",
) -> str:
    """Cancel a pending deploy request, returning the artifact to DRAFT.

    Drives the transition through the platform governance router so the cached
    status (Redis → memory → Postgres) all flip together — a raw Postgres write
    would leave the badge showing "Awaiting Approval" until the cache expired.
    Falls back to a direct mirror-row write if the router isn't importable
    (standalone ABStudio). Returns the resulting status.

    Only valid while the artifact is still PENDING_APPROVAL / PENDING_L2; the
    router raises a 409 otherwise, which the API layer surfaces to the user.
    """
    current = get_governance_status(entity_type, name, owner_id=created_by)
    if current not in ("PENDING_APPROVAL", "PENDING_L2"):
        # Nothing to cancel — already draft/approved/rejected. Idempotent no-op.
        return current or "DRAFT"

    # Preferred path: reuse the platform router's transition so every status
    # cache is updated and the audit/notify events fire.
    try:
        from routers.governance_router import _transition, _governance_notify, _bg
        from_status, to_status = _transition(
            entity_type, name, "withdraw",
            actor or created_by or "unknown",
            extra_updates={"withdrawn_by": actor or created_by or "unknown"},
            owner_id=created_by,
        )
        _bg(_governance_notify, entity_type, name, "withdraw",
            from_status, to_status, actor or created_by or "unknown",
            owner_id=created_by)
        return to_status
    except Exception as e:
        logger.warning(
            f"[AGENT] withdraw via platform router failed for {entity_type}/{name}: {e}; "
            f"falling back to direct mirror write"
        )

    # Fallback: standalone ABStudio without the platform router mounted.
    _upsert_record(
        entity_type, name,
        status="DRAFT",
        created_by=created_by,
        department="",
    )
    _notify_and_record(entity_type, name, "withdraw", current, "DRAFT",
                       actor=actor or created_by or "unknown",
                       owner_id=created_by)
    return "DRAFT"


def normalize_visibility(value: Optional[str]) -> str:
    """Coerce a caller-supplied visibility to 'public' or 'private' (default)."""
    v = (value or "private").strip().lower()
    return v if v in ("public", "private") else "private"


async def submit_skill_async(
    name: str,
    *,
    content: str,
    created_by: str = "",
    actor: str = "",
    department: str = "",
    description: str = "",
    visibility: Optional[str] = None,
) -> None:
    """Submit a skill for HOD approval off the event loop.

    Shared by the AI-generation confirm path and the zip-upload importer so the
    normalization + threading + failure logging live in one place. A failed
    submit is a governance-control failure, so it's logged at warning (not
    silently at debug) while still not breaking the create/upload response.
    """
    import asyncio
    vis = normalize_visibility(visibility)

    def _submit():
        submit_for_governance(
            "skills", name,
            created_by=created_by,
            actor=actor,
            department=department,
            description=description,
            visibility=vis,
            source_template_hash=canonical_hash(content),
        )

    try:
        await asyncio.to_thread(_submit)
    except Exception:
        logger.warning(
            f"[AGENT] governance submit failed for skills/{name} — "
            f"skill saved but NOT submitted for approval",
            exc_info=True,
        )


def mark_approved_template_instance(
    entity_type: str,
    name: str,
    *,
    created_by: str = "",
    department: str = "",
    description: str = "",
    source_template_id: Optional[str] = None,
    source_template_hash: Optional[str] = None,
) -> str:
    """Register an UNMODIFIED template instance as pre-approved (no approval needed).

    Pre-existing templates are trusted, so instantiating one yields an
    artifact that is immediately usable (status PRODUCTION). The
    ``source_template_hash`` is stored so a later edit can be detected as a
    modification that DOES require approval.
    """
    _upsert_record(
        entity_type, name,
        status="PRODUCTION",
        created_by=created_by,
        department=department,
        description=description,
        source_template_id=source_template_id,
        source_template_hash=source_template_hash,
        last_approved_hash=source_template_hash,
        approved_by="template",
    )
    _notify_and_record(entity_type, name, "promote", "DRAFT", "PRODUCTION",
                       actor="template", notify=False, owner_id=created_by)
    return "PRODUCTION"


# ---------------------------------------------------------------------------
# Update path — decide whether an edit needs re-approval
# ---------------------------------------------------------------------------

def reconcile_after_update(
    entity_type: str,
    name: str,
    current_content: Any,
    *,
    created_by: str = "",
    department: str = "",
    description: str = "",
    owner_id: str = "",
) -> Optional[str]:
    """Called after an artifact is edited/saved (incl. autosave).

    IMPORTANT: this NEVER submits for approval or notifies anyone. Submission is
    always an explicit user action (the "Submit for Approval" button). This hook
    only *demotes* an artifact that was already APPROVED/PRODUCTION back to DRAFT
    when its content actually changed, so a previously-approved artifact can't be
    silently edited and still run. Ungoverned artifacts (no record) are left
    untouched — editing a brand-new workflow must not create a governance record
    or fire notifications.

    Returns the (possibly new) status, or None if there was nothing to do.
    """
    mapping = _MODEL_MAP.get(entity_type)
    if not mapping:
        return None
    new_hash = canonical_hash(current_content)
    try:
        import importlib
        from db.database import SessionLocal
        mod = importlib.import_module(mapping[0])
        Model = getattr(mod, mapping[1])
        db = SessionLocal()
        try:
            q = db.query(Model).filter(Model.name == name)
            _owner = owner_id or created_by
            if _owner:
                q = q.filter(Model.created_by == _owner)
            row = q.first()
            if row is None:
                return None  # ungoverned artifact — nothing to reconcile
            status = getattr(row, "status", None)
            approved_hash = getattr(row, "last_approved_hash", None)
            template_hash = getattr(row, "source_template_hash", None)

            # Only act on artifacts currently in a usable state. Drafts and
            # already-pending items don't need demoting.
            if status not in USABLE_STATUSES:
                return status

            unchanged = new_hash in {h for h in (approved_hash, template_hash) if h}
            if unchanged:
                return status  # no real change — keep approved

            # Content genuinely changed on an approved artifact: revoke approval
            # (silent — no notification). The user must click Submit again.
            row.status = "DRAFT"
            row.source_template_hash = new_hash
            db.commit()
            return "DRAFT"
        finally:
            db.close()
    except Exception as e:
        logger.debug(f'[AGENT] reconcile_after_update failed for {entity_type}/{name}: {e}')
        return None


# ---------------------------------------------------------------------------
# Notification / audit-event bridge to the platform governance router
# ---------------------------------------------------------------------------

def _notify_and_record(entity_type, name, action, from_status, to_status,
                       *, actor="unknown", reason=None, notify=True,
                       owner_id: str = ""):
    """Reuse the governance router's inbox-notify + signed-audit-event helpers."""
    try:
        from routers.governance_router import _pg_record_event, _governance_notify, _bg
        _bg(_pg_record_event, entity_type, name, action, actor, from_status, to_status, reason, owner_id=owner_id)
        if notify:
            _bg(_governance_notify, entity_type, name, action, from_status, to_status, actor, reason, owner_id=owner_id)
    except Exception as e:
        logger.debug(f'[AGENT] governance notify/record bridge unavailable: {e}')
