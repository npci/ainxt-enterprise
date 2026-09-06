# SPDX-License-Identifier: MIT
# ============================================================
# GOVERNANCE ROUTER — lifecycle management for Agents, Skills, MCP tools, Workflows
#
# Lifecycle: DRAFT → PENDING_APPROVAL → APPROVED → PRODUCTION
#            PENDING_APPROVAL → REJECTED (→ re-submit → PENDING_APPROVAL)
#            Any PRODUCTION → DEPRECATED
#
# Storage:   Redis (live state) + Postgres governance_events (durable audit log)
#            Postgres agents_pg/skills_pg/mcp_servers/workflows_pg.status columns
#            are the authoritative source on restart.
#
# Endpoints:
#   GET  /governance/{entity_type}                  list all with governance fields + pagination
#   POST /governance/{entity_type}/{name}/submit     → PENDING_APPROVAL (any authenticated user)
#   POST /governance/{entity_type}/{name}/approve    → APPROVED  (approver roles only)
#   POST /governance/{entity_type}/{name}/reject     → REJECTED  (approver roles only)
#   POST /governance/{entity_type}/{name}/promote    → PRODUCTION (approver roles only)
#   POST /governance/{entity_type}/{name}/deprecate  → DEPRECATED (approver roles only)
#   GET  /governance/{entity_type}/{name}/history    durable audit log (always returns list)
# ============================================================

import json
import threading
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from core.security_validation import validate_identifier, validate_free_text, _flatten_errors

from core.config import (
    REDIS_HOST as _REDIS_HOST, REDIS_PORT as _REDIS_PORT, RDB_WORKFLOW, RDB_REGISTRY,
    HOD_APPROVAL_ENABLED,
)
from core.kv import get_kv, KVError
from core.logger import logger
from auth.dependencies import get_current_user


def _bg(fn, *args, **kwargs):
    """Fire-and-forget: run fn(*args, **kwargs) in a daemon thread.
    Any exception is logged and swallowed — caller is never blocked."""
    def _run():
        try:
            fn(*args, **kwargs)
        except Exception as _e:
            logger.warning(f"_bg({fn.__name__}): {_e}")
    threading.Thread(target=_run, daemon=True, name=f"bg-{fn.__name__}").start()


def _to_ist_str() -> str:
    """Return current time formatted as IST (UTC+5:30) for inbox messages."""
    from datetime import datetime as _dt, timedelta as _td
    return (_dt.utcnow() + _td(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p IST")

router = APIRouter(prefix="/governance", tags=["governance"])

# ── Valid entity types ────────────────────────────────────────
ENTITY_TYPES = {"agents", "skills", "mcp", "workflows"}

# ── Roles allowed to approve / reject / promote / deprecate ──
APPROVER_ROLES = {"admin", "platform_engineer", "security"}

# ── State machine: which transitions are allowed ─────────────
_VALID_TRANSITIONS = {
    "submit":    (("DRAFT", "REJECTED"),              "PENDING_APPROVAL"),
    "approve":   (("PENDING_APPROVAL", "PENDING_L2"), "APPROVED"),
    "reject":    (("PENDING_APPROVAL", "PENDING_L2"), "REJECTED"),
    "promote":   (("APPROVED",),                      "PRODUCTION"),
    "deprecate": (("PRODUCTION", "APPROVED"),         "DEPRECATED"),
    # Owner-initiated cancel of a pending deploy request: returns the artifact
    # to an editable DRAFT so the submitter can keep working on it.
    "withdraw":  (("PENDING_APPROVAL", "PENDING_L2"), "DRAFT"),
}

# ── IS team departments for L2 MCP approval (configurable) ───
import os as _os
_IS_TEAM_DEPTS = set(
    d.strip() for d in _os.environ.get("IS_TEAM_DEPARTMENTS", "IS,AppSec,InfoSec").split(",") if d.strip()
)

# ── Redis prefixes (must match other modules) ─────────────────
_REDIS_PREFIXES = {
    "agents":    "agent_builder:agent:",
    "skills":    "skill_store:",
    "mcp":       "mcp:tool:",
    "workflows": "workflow_store:",
}
_REDIS_INDICES = {
    "agents":    "agent_builder:index",
    "skills":    "skill_store:index",
    "mcp":       "mcp:tool:index",
    "workflows": "workflow_store:index",
}


# ============================================================
# KV HELPERS
# Governance entity cache lives in DB=2 (workflow KV).
# Marketplace status sync lives in DB=3 (registry KV).
# Backends selected via REDIS_CLIENT_CONFIG_DB2 / _DB3.
# ============================================================

def _get_redis():
    """KV client for governance entity cache (DB=2)."""
    try:
        c = get_kv(RDB_WORKFLOW, decode_responses=True)
        c.ping()
        return c
    except KVError:
        return None


def _get_marketplace_kv():
    """KV client for the marketplace tool registry (DB=3)."""
    try:
        c = get_kv(RDB_REGISTRY, decode_responses=True)
        c.ping()
        return c
    except KVError:
        return None


def _sync_marketplace_status(name: str, new_status: str) -> None:
    """Update the status field of marketplace:tool:{name} (best-effort, no-op on KV failure)."""
    try:
        kv = _get_marketplace_kv()
        if kv is None:
            return
        raw = kv.get(f"marketplace:tool:{name}")
        if not raw:
            return
        try:
            doc = json.loads(raw)
        except Exception:
            return
        doc["status"] = new_status
        kv.set(f"marketplace:tool:{name}", json.dumps(doc))
    except Exception as e:
        logger.warning(f"governance_router: marketplace status sync failed: {e}")


def _load_entity_redis(entity_type: str, name: str, *, owner_id: str = "") -> Optional[dict]:
    r = _get_redis()
    if r is None:
        return None
    raw = r.get(f"{_REDIS_PREFIXES.get(entity_type, entity_type + ':'  )}{name}")
    if not raw:
        return None
    data = json.loads(raw)
    # When owner_id is provided, verify the cached record belongs to this owner.
    # Without this, two users with same-named artifacts would share a Redis
    # governance cache entry on a shared database.
    if owner_id and data.get("created_by", "") and data.get("created_by", "") != owner_id:
        return None
    return data


def _save_entity_redis(entity_type: str, name: str, data: dict) -> None:
    r = _get_redis()
    if r is None:
        return
    prefix = _REDIS_PREFIXES.get(entity_type, f"{entity_type}:")
    r.set(f"{prefix}{name}", json.dumps(data))
    r.sadd(_REDIS_INDICES.get(entity_type, f"{entity_type}:index"), name)


def _list_entities_redis(entity_type: str) -> List[dict]:
    r = _get_redis()
    if r is None:
        return []
    idx    = _REDIS_INDICES.get(entity_type, f"{entity_type}:index")
    prefix = _REDIS_PREFIXES.get(entity_type, f"{entity_type}:")
    names  = r.smembers(idx) or set()
    result = []
    for name in names:
        raw = r.get(f"{prefix}{name}")
        if raw:
            try:
                result.append(json.loads(raw))
            except Exception:
                pass
    return result


# ============================================================
# POSTGRES HELPERS
# ============================================================

def _pg_record_event(entity_type: str, name: str, action: str, actor: str,
                     from_status: Optional[str], to_status: str,
                     reason: Optional[str] = None, *, owner_id: str = "") -> None:
    """Persist an immutable governance event to Postgres."""
    try:
        from db.database import SessionLocal
        from db.models import GovernanceEvent
        db = SessionLocal()
        try:
            from core.audit_signer import sign_event
            from datetime import datetime as _dt
            _now_iso = _dt.utcnow().isoformat()
            _event_dict = {
                "entity_type": entity_type,
                "name":        name,
                "action":      action,
                "from_status": from_status,
                "to_status":   to_status,
                "actor":       actor,
                "reason":      reason,
                "created_at":  _now_iso,
            }
            event = GovernanceEvent(
                entity_type=entity_type,
                name=name,
                action=action,
                from_status=from_status,
                to_status=to_status,
                actor=actor,
                reason=reason,
                created_by=owner_id or None,
                signature=sign_event(_event_dict),
            )
            db.add(event)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"governance_router: Postgres event persist failed: {e}")


def _pg_update_status(entity_type: str, name: str, updates: dict, *, owner_id: str = "") -> bool:
    """Sync governance fields to the Postgres entity table."""
    _TABLE_MAP = {
        "agents":    ("db.models", "AgentRecord"),
        "skills":    ("db.models", "SkillRecord"),
        "mcp":       ("db.models", "MCPServer"),
        "workflows": ("db.models", "WorkflowRecord"),
    }
    mapping = _TABLE_MAP.get(entity_type)
    if not mapping:
        return False
    try:
        import importlib
        from db.database import SessionLocal
        mod   = importlib.import_module(mapping[0])
        Model = getattr(mod, mapping[1])
        db    = SessionLocal()
        try:
            q = db.query(Model).filter(Model.name == name)
            if owner_id and hasattr(Model, "created_by"):
                q = q.filter(Model.created_by == owner_id)
            row = q.first()
            if row:
                for k, v in updates.items():
                    if hasattr(row, k):
                        setattr(row, k, v)
                db.commit()
                return True
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"governance_router: Postgres status sync failed for {entity_type}/{name}: {e}")
    return False


def _pg_get_history(entity_type: str, name: str, *, owner_id: str = "") -> List[dict]:
    """Fetch governance history from Postgres."""
    try:
        from db.database import SessionLocal
        from db.models import GovernanceEvent
        db = SessionLocal()
        try:
            q = (db.query(GovernanceEvent)
                 .filter(GovernanceEvent.entity_type == entity_type,
                         GovernanceEvent.name == name))
            if owner_id:
                q = q.filter(GovernanceEvent.created_by == owner_id)
            rows = q.order_by(GovernanceEvent.created_at.asc()).all()
            return [
                {
                    "action":      r.action,
                    "from_status": r.from_status,
                    "to_status":   r.to_status,
                    "actor":       r.actor,
                    "reason":      r.reason,
                    "timestamp":   r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"governance_router: Postgres history fetch failed: {e}")
    return []


# ============================================================
# IN-MEMORY HELPERS (agent_builder singleton)
# ============================================================

def _get_agent_builder():
    try:
        from agents.agent_builder import agent_builder
        return agent_builder
    except Exception:
        return None


def _get_mcp_registry():
    try:
        from mcp.registry import mcp_registry
        return mcp_registry
    except Exception:
        return None


def _get_entity_status(entity_type: str, name: str, *, owner_id: str = "") -> Optional[str]:
    # 1. Redis
    data = _load_entity_redis(entity_type, name, owner_id=owner_id)
    if data:
        return data.get("status", "PRODUCTION")
    # 2. In-memory agent builder
    if entity_type == "agents":
        ab = _get_agent_builder()
        if ab:
            agent = ab.get(name)
            if agent:
                return getattr(agent, "status", "PRODUCTION")
    # 3. Postgres
    _TABLE_MAP = {
        "agents":    ("db.models", "AgentRecord"),
        "skills":    ("db.models", "SkillRecord"),
        "mcp":       ("db.models", "MCPServer"),
        "workflows": ("db.models", "WorkflowRecord"),
    }
    mapping = _TABLE_MAP.get(entity_type)
    if mapping:
        try:
            import importlib
            from db.database import SessionLocal
            mod   = importlib.import_module(mapping[0])
            Model = getattr(mod, mapping[1])
            db    = SessionLocal()
            try:
                q = db.query(Model).filter(Model.name == name)
                if owner_id and hasattr(Model, "created_by"):
                    q = q.filter(Model.created_by == owner_id)
                row = q.first()
                if row:
                    return getattr(row, "status", "PRODUCTION")
            finally:
                db.close()
        except Exception:
            pass
    return None


def _set_entity_governance(entity_type: str, name: str, updates: dict, *, owner_id: str = "") -> bool:
    """Apply governance field updates to Redis + in-memory + Postgres."""
    data = _load_entity_redis(entity_type, name, owner_id=owner_id)
    if data is None:
        if entity_type == "agents":
            ab = _get_agent_builder()
            if ab and name in ab:
                import dataclasses
                data = dataclasses.asdict(ab.get(name))
        if data is None:
            # Entity lives only in Postgres (no Redis copy) — update DB directly
            updated = _pg_update_status(entity_type, name, updates, owner_id=owner_id)
            return updated

    data.update(updates)
    _save_entity_redis(entity_type, name, data)

    # Sync in-memory agent builder
    if entity_type == "agents":
        try:
            from agents.agent_builder import agent_builder
            if name in agent_builder:
                agent = agent_builder.get(name)
                for k, v in updates.items():
                    if hasattr(agent, k):
                        setattr(agent, k, v)
        except Exception as e:
            logger.warning(f"governance_router: in-memory agent sync failed: {e}")

    # Sync Postgres
    _pg_update_status(entity_type, name, updates, owner_id=owner_id)
    return True


# ============================================================
# INBOX NOTIFICATION HELPER
# ============================================================

def _get_entity_department(entity_type: str, name: str, *, owner_id: str = "") -> str:
    """Return the department stored on the governance record, or empty string.

    Governance records (agents_pg/skills_pg/workflows_pg) carry a ``department``
    column populated at submit time from the creator's JWT department. This is
    the basis for HOD (department-manager) approval routing.
    """
    _MODEL_ATTR = {
        "agents":    ("db.models", "AgentRecord"),
        "mcp":       ("db.models", "AgentRecord"),
        "skills":    ("db.models", "SkillRecord"),
        "workflows": ("db.models", "WorkflowRecord"),
    }
    mapping = _MODEL_ATTR.get(entity_type)
    if not mapping:
        return ""
    try:
        import importlib
        from db.database import SessionLocal
        mod   = importlib.import_module(mapping[0])
        Model = getattr(mod, mapping[1])
        db    = SessionLocal()
        try:
            q = db.query(Model).filter(Model.name == name)
            if owner_id and hasattr(Model, "created_by"):
                q = q.filter(Model.created_by == owner_id)
            row = q.first()
            return (getattr(row, "department", "") or "") if row else ""
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"_get_entity_department failed for {entity_type}/{name}: {e}")
        return ""


def _get_entity_visibility(entity_type: str, name: str, *, owner_id: str = "") -> str:
    """Return the requested catalog visibility on the governance record.

    Set at submit time (the "Deploy" request). Defaults to 'public' when absent.
    """
    _MODEL_ATTR = {
        "agents":    ("db.models", "AgentRecord"),
        "skills":    ("db.models", "SkillRecord"),
        "workflows": ("db.models", "WorkflowRecord"),
    }
    mapping = _MODEL_ATTR.get(entity_type)
    if not mapping:
        return "public"
    try:
        import importlib
        from db.database import SessionLocal
        mod   = importlib.import_module(mapping[0])
        Model = getattr(mod, mapping[1])
        db    = SessionLocal()
        try:
            q = db.query(Model).filter(Model.name == name)
            if owner_id and hasattr(Model, "created_by"):
                q = q.filter(Model.created_by == owner_id)
            row = q.first()
            return (getattr(row, "visibility", "public") or "public") if row else "public"
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"_get_entity_visibility failed for {entity_type}/{name}: {e}")
        return "public"


def _publish_as_template(entity_type: str, artifact_name: str, *, owner_id: str = ""):
    """Publish an approved Build Studio workflow/agent into the shared catalog.

    Reads the requested visibility + creator department off the governance mirror
    record and copies the artifact into the ABStudio templates catalog. Runs in a
    background thread (via ``_bg``) off the approval path; best-effort so a
    publish hiccup never fails the approval.
    """
    try:
        import asyncio as _asyncio
        from app.core import workflow_repo as _wr
        visibility = _get_entity_visibility(entity_type, artifact_name, owner_id=owner_id)
        department = _get_entity_department(entity_type, artifact_name, owner_id=owner_id) or None
        publish = (_wr.publish_workflow_as_template if entity_type == "workflows"
                   else _wr.publish_agent_as_template)
        tid = _asyncio.run(publish(artifact_name, visibility=visibility, department=department))
        logger.info("governance: published %s/%s as template %s (visibility=%s)",
                    entity_type, artifact_name, tid, visibility)

        # Once an agent is published as a shared template it should live ONLY in
        # the Templates catalog, not under the submitter's "My Agents". Delete
        # the source agent row (and its triggers/chat) so it disappears from the
        # dashboard. Best-effort and sequenced AFTER a successful publish so a
        # cleanup hiccup never blocks approval or loses the template.
        if entity_type == "agents" and tid and owner_id:
            _delete_published_source_agent(artifact_name, owner_id)
    except Exception as e:
        logger.warning("governance: publish-as-template failed for %s/%s: %s",
                       entity_type, artifact_name, e)


def _delete_published_source_agent(artifact_name: str, owner_id: str) -> None:
    """Remove the source agent (and dependent triggers/chat) after it has been
    published as a template. Mirrors the cleanup in the ABStudio
    ``DELETE /agents/{id}`` route so no orphan schedules point at the deleted
    agent. Best-effort: every failure is logged, never raised."""
    try:
        import asyncio as _asyncio
        from app.core import workflow_repo as _wr

        async def _run():
            agent = await _wr.get_agent_by_name(artifact_name, owner_id)
            if not agent:
                logger.info("governance: no source agent %r for owner %s to delete "
                            "after publish (already gone?)", artifact_name, owner_id)
                return
            agent_id = agent.get("id")
            # Deregister + drop triggers targeting this agent.
            try:
                from app.services import trigger_scheduler as _ts
                existing = await _wr.list_triggers(owner_id, "agent", agent_id)
                for t in existing:
                    try:
                        _ts.deregister_trigger(t["id"])
                    except Exception:
                        logger.debug("governance: deregister trigger %s failed", t.get("id"))
                await _wr.delete_triggers_for_target("agent", agent_id)
            except Exception:
                logger.warning("governance: trigger cleanup failed for published agent %s",
                               agent_id, exc_info=True)
            # Delete the agent row itself.
            deleted = await _wr.delete_agent(agent_id, owner_id)
            # Drop the agent's chat threads (best-effort).
            try:
                from app.api import agent_chat as _ac
                await _ac.get_store().delete_threads_for_agent(agent_id, owner_id)
            except Exception:
                logger.debug("governance: agent chat cleanup skipped for %s", agent_id, exc_info=True)
            logger.info("governance: removed source agent %s (%r) from My Agents after "
                        "publish (deleted=%s)", agent_id, artifact_name, deleted)

        _asyncio.run(_run())
    except Exception as e:
        logger.warning("governance: failed to delete source agent %r after publish: %s",
                       artifact_name, e)


def _resolve_hod_user_ids(department: str) -> List[str]:
    """Resolve the HOD (department manager) user id(s) for a department name.

    Reuses the DBA-owned ``ainxt.department_hod_mapping`` view (case-sensitive
    department match, case-insensitive email match) that the rest of the
    platform's HOD scoping relies on. Returns active HOD users' string ids.
    Empty list if the department has no HOD mapped — callers should fall back
    to the senior-approver broadcast.

    Flat mode (HOD_APPROVAL_ENABLED=False, the default): always returns []
    regardless of department, unconditionally. _require_scoped_approver's own
    pre-existing "no HOD mapped -> admin-only" fallback then applies to every
    private item, from every department — i.e. every private
    workflow/skill/agent submission becomes approvable by an admin only. See
    the module-level plan doc for the full mechanics.
    """
    if not HOD_APPROVAL_ENABLED:
        return []
    if not department:
        return []
    try:
        from db.database import SessionLocal
        from db.models import User, DepartmentHodMapping
        from sqlalchemy import func
        db = SessionLocal()
        try:
            rows = (
                db.query(DepartmentHodMapping.hod_email)
                .filter(DepartmentHodMapping.department_name == department)
                .all()
            )
            emails = [r[0] for r in rows if r and r[0]]
            if not emails:
                return []
            lowered = [e.lower() for e in emails]
            users = (
                db.query(User.id)
                .filter(func.lower(User.email).in_(lowered),
                        User.is_active == True)
                .all()
            )
            return [str(u[0]) for u in users]
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"_resolve_hod_user_ids failed for '{department}': {e}")
        return []


def _resolve_admin_user_ids() -> List[str]:
    """Return the string ids of every active ``admin`` user.

    Used as the fallback approver set whenever a department has no HOD mapped.
    Deliberately does NOT include ``ad_level <= 3`` seniors — approval must go
    to the mapped HOD or an admin, never to unrelated seniors.
    """
    try:
        from db.database import SessionLocal
        from db.models import User
        db = SessionLocal()
        try:
            admins = db.query(User.id).filter(
                User.role == "admin",
                User.is_active == True,
            ).all()
            return [str(u[0]) for u in admins]
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"_resolve_admin_user_ids failed: {e}")
        return []


def _resolve_approval_recipients(department: str, visibility: str = "private") -> List[str]:
    """Approval-notification recipient set, matching the approval-guard rules.

    Single source of truth shared by the submit-time notification and the SLA
    overdue reminder so both route identically to the scoped-approver guard:

      - ``public``  -> admins only.
      - ``private`` -> the mapped HOD(s) for ``department`` when present,
        otherwise every active admin.
    """
    if (visibility or "private").strip().lower() == "public":
        return _resolve_admin_user_ids()
    hod_ids = _resolve_hod_user_ids(department)
    return hod_ids if hod_ids else _resolve_admin_user_ids()


def _resolve_sent_to_label(visibility: str, department: str) -> str:
    """Return a human-readable label for who the approval request was sent to.

    Used only in the inbox message body — routing logic is owned by
    _resolve_approval_recipients.
      public          → "Admin"
      private + HOD   → HOD name(s) from department_hod_mapping.hod_name,
                        falling back to hod_email prefix if hod_name is blank
      private + no HOD→ "Admin" (mirrors the routing fallback)
    """
    if (visibility or "private").strip().lower() == "public":
        return "Admin"
    if not department:
        return "Admin"
    try:
        from db.database import SessionLocal
        from db.models import DepartmentHodMapping
        db = SessionLocal()
        try:
            rows = (
                db.query(DepartmentHodMapping.hod_name, DepartmentHodMapping.hod_email)
                .filter(DepartmentHodMapping.department_name == department)
                .all()
            )
        finally:
            db.close()
        names = []
        for row in rows:
            label = (row.hod_name or "").strip()
            if not label and row.hod_email:
                label = row.hod_email.split("@")[0]
            if label:
                names.append(label)
        if names:
            return ", ".join(names)
    except Exception as _e:
        logger.debug(f"_resolve_sent_to_label failed for dept='{department}': {_e}")
    return "Admin"


def _resolve_approver_display_names(department: str, visibility: str) -> List[str]:
    """Human-readable names of the approvers a submission was routed to.

    Reuses ``_resolve_approval_recipients`` (same set the approval guard and
    inbox notification use) so the names match the people who actually
    receive the inbox item. Returns display names (name → email → id),
    deduped, empty list on any failure.
    """
    try:
        from db.database import SessionLocal
        from db.models import User
        recipient_ids = _resolve_approval_recipients(department, visibility)
        if not recipient_ids:
            return []
        # ``User.id`` is a UUID string — ``_resolve_approval_recipients``
        # already returns string ids, so filter blanks and query directly.
        # (A previous version cast to int, which threw on UUIDs and silently
        # returned [] — producing the generic "your department manager /
        # admins" fallback instead of real names.)
        clean_ids = [str(i) for i in recipient_ids if i]
        db = SessionLocal()
        try:
            users = db.query(User).filter(User.id.in_(clean_ids)).all()
            names = []
            for u in users:
                label = (u.name or "").strip() or (u.email or "").strip() or str(u.id)
                if label and label not in names:
                    names.append(label)
            return names
        finally:
            db.close()
    except Exception:
        return []


def _governance_notify(entity_type: str, name: str, action: str,
                       from_status: str, to_status: str, actor: str,
                       reason: Optional[str] = None, *, owner_id: str = "") -> None:
    """Push inbox notification to approvers (on submit) or entity creator (on approve/reject/etc)."""
    try:
        from store.inbox_store import publish_inbox_item, delete_pending_by_source
        from db.database import SessionLocal
        from db.models import User

        _entity_label = entity_type.rstrip("s").title()
        _ist = _to_ist_str()
        _reject_body = (
                f"**{_entity_label} `{name}`** was **rejected** by `{actor}` on {_ist}."
                + (f"\n\n**Reason:** {reason}" if reason else "")
        )
        # Resolve who the approval is being sent to for the inbox message.
        # We do this early (before the submit-branch reads department/visibility)
        # so we can embed it in the body. The routing itself is still owned by
        # _resolve_approval_recipients — this is display-only.
        _submit_dept       = _get_entity_department(entity_type, name, owner_id=owner_id)
        _submit_visibility = _get_entity_visibility(entity_type, name, owner_id=owner_id)
        _sent_to_label     = _resolve_sent_to_label(_submit_visibility, _submit_dept)
        # Scope label the maker/approver see in every governance message
        # (public vs department). We render department in parentheses when
        # available so the reader knows exactly which cohort will act on it,
        # without needing to open the entity.
        _scope_label = "Public"
        if (_submit_visibility or "private").strip().lower() != "public":
            _scope_label = "Department" + (f" ({_submit_dept})" if _submit_dept else "")
        _submit_body = (
                f"**{_entity_label} `{name}`** submitted for approval by `{actor}` on {_ist}."
                + (f"\n\n**Reason:** {reason}" if reason else "")
                + f"\n\n**Scope:** {_scope_label}"
                + f"\n**Sent to:** {_sent_to_label}"
        )
        _msgs = {
            "submit":     _submit_body,
            "approve":    f"**{_entity_label} `{name}`** was **approved** by `{actor}` on {_ist}. Ready to promote to PRODUCTION.",
            "approve_l1": f"**{_entity_label} `{name}`** passed L1 review by `{actor}` on {_ist}. Awaiting IS/Security team approval.",
            "reject":     _reject_body,
            "promote":    f"**{_entity_label} `{name}`** is now **LIVE in PRODUCTION** (promoted by `{actor}` on {_ist}).",
            "deprecate":  f"**{_entity_label} `{name}`** has been deprecated by `{actor}` on {_ist}.",
            "withdraw":   f"**{_entity_label} `{name}`** — the deploy request was **cancelled** by `{actor}` on {_ist}. It is editable again.",
        }
        body = _msgs.get(action, f"{entity_type} {name}: {from_status} → {to_status}.")

        if action == "submit":
            # Route the approval request the same way the approval guard
            # authorizes it (see _require_scoped_approver):
            #   public  -> admins only
            #   private -> mapped HOD if present, else admins
            # We deliberately do NOT broadcast to every senior (ad_level ≤ 3)
            # user — that leaked approval requests to unrelated departments.
            # department + visibility already resolved above for the body label.
            department  = _submit_dept
            visibility  = _submit_visibility
            _meta = {"entity_type": entity_type, "entity_name": name,
                     "action": action, "status": to_status,
                     # ``current_status`` mirrors ``status`` so the frontend
                     # (Inbox.jsx UniversalInboxActions) can read either key.
                     "current_status": to_status,
                     "submitted_by": actor, "department": department,
                     # Scope fields let the inbox UI render a Public /
                     # Department chip without re-parsing the body text.
                     "visibility": (visibility or "private"),
                     "sent_to": _sent_to_label,
                     "owner_id": owner_id}
            recipients = _resolve_approval_recipients(department, visibility)
            # Resubmit freshness: if this artifact was previously submitted
            # (then cancelled / rejected) any stale PENDING inbox rows for the
            # same recipients would linger and show an outdated timestamp/body.
            # Delete them before publishing the new row so approvers always see
            # the newest submit as a fresh notification. Approve/reject audit
            # rows are preserved (delete_pending_by_source only removes rows
            # whose metadata.status is PENDING_*).
            try:
                delete_pending_by_source(recipients, "governance_approval", name)
            except Exception as _e:
                logger.debug(f"_governance_notify: stale-pending cleanup skipped: {_e}")
            for uid in recipients:
                publish_inbox_item(
                    user_id  = uid,
                    type     = "governance_approval",
                    title    = f"[Needs Approval] {_entity_label}: {name}",
                    body     = body,
                    source_id= name,
                    metadata = _meta,
                )
            # Also drop a confirmation inbox item into the MAKER's inbox so
            # they have a visible trail of their own submission (who it went
            # to, current status). Without this a non-admin maker sees only
            # the transient submit toast and no record in their inbox. Always
            # sent — even when the maker is also an approver (e.g. maker-
            # admin): the [Needs Approval] item is an action request, the
            # [Submitted] item is a submission trail, and both are legitimate.
            # The two have distinct titles/metadata so the inbox can render
            # them differently (the maker-check hides Approve/Reject on the
            # maker's own [Needs Approval] item).
            creator_id = _get_entity_owner_id(entity_type, name, owner_id=owner_id)
            if creator_id:
                approver_names = _resolve_approver_display_names(department, visibility)
                who = (", ".join(approver_names) if approver_names
                       else "your department manager / admins")
                publish_inbox_item(
                    user_id  = creator_id,
                    type     = "governance_approval",
                    title    = f"[Submitted] {_entity_label}: {name}",
                    body     = (
                        f"**{_entity_label} `{name}`** was submitted for approval by `{actor}` on {_ist}."
                        f"\n\n**Sent to:** {who}"
                        + (f"\n\n**Reason:** {reason}" if reason else "")
                    ),
                    source_id= name,
                    metadata = _meta,
                )
        else:
            # Non-submit outcomes (approve / reject / promote / deprecate /
            # withdraw). We write TWO persisted rows:
            #   1. Creator — so the maker always sees the outcome of their
            #      request in their inbox.
            #   2. Actor — so the approver retains an audit trail of what they
            #      did. Without this, approvers who acted on a department-scoped
            #      item (they saw it via the live pending-approvals view but
            #      had no persisted PENDING row) would lose the row entirely
            #      the moment the status flipped out of PENDING_APPROVAL,
            #      because live rows filter on status='PENDING_APPROVAL'.
            _meta = {"entity_type": entity_type, "entity_name": name,
                     "action": action, "status": to_status,
                     "current_status": to_status, "actor": actor,
                     "owner_id": owner_id}
            _title = f"[{to_status}] {_entity_label}: {name}"

            creator_id = _get_entity_owner_id(entity_type, name, owner_id=owner_id)
            if creator_id:
                publish_inbox_item(
                    user_id  = creator_id,
                    type     = "governance_approval",
                    title    = _title,
                    body     = body,
                    source_id= name,
                    metadata = _meta,
                )

            # Look up the actor's User.id so we can write to their inbox too.
            # ``actor`` is a display string (email or full name); the user id
            # is what publish_inbox_item keys on. Skip cleanly on any lookup
            # miss — never fail the whole notify path.
            _actor_uid = ""
            try:
                _adb = SessionLocal()
                try:
                    _actor_str = (actor or "").strip()
                    if _actor_str:
                        _u = None
                        if "@" in _actor_str:
                            _u = _adb.query(User).filter(User.email == _actor_str.lower()).first()
                        if _u is None:
                            _u = _adb.query(User).filter(User.full_name == _actor_str).first()
                        if _u is not None:
                            _actor_uid = str(_u.id)
                finally:
                    _adb.close()
            except Exception as _e:
                logger.debug(f"_governance_notify: actor user-id lookup failed: {_e}")

            # Withdraw is initiated by the creator themselves — no separate
            # actor row needed (would be a duplicate of the creator row).
            if _actor_uid and _actor_uid != str(creator_id or "") and action != "withdraw":
                publish_inbox_item(
                    user_id  = _actor_uid,
                    type     = "governance_approval",
                    title    = _title,
                    body     = body,
                    source_id= name,
                    metadata = _meta,
                )
    except Exception as _e:
        logger.warning(f"_governance_notify failed: {_e}")


def _notify_is_team_for_l2(entity_type: str, name: str, l1_actor: str) -> None:
    """Notify IS/AppSec/InfoSec team users when an MCP tool needs L2 approval."""
    try:
        from store.inbox_store import publish_inbox_item
        from db.database import SessionLocal
        from db.models import User
        db = SessionLocal()
        try:
            is_users = db.query(User).filter(
                User.department.in_(list(_IS_TEAM_DEPTS)),
                User.is_active == True,
                ).all()
            # Also include admins
            admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
            notified = set()
            for u in list(is_users) + list(admins):
                uid = str(u.id)
                if uid in notified:
                    continue
                notified.add(uid)
                publish_inbox_item(
                    user_id   = uid,
                    type      = "governance_approval",
                    title     = f"[IS Review Required] MCP Tool: {name}",
                    body      = f"**MCP Tool `{name}`** passed L1 review by `{l1_actor}` and requires IS/Security team (L2) approval.",
                    source_id = name,
                    metadata  = {"entity_type": entity_type, "entity_name": name,
                                 "action": "approve_l2", "status": "PENDING_L2",
                                 "l1_approved_by": l1_actor},
                )
        finally:
            db.close()
    except Exception as _e:
        logger.warning(f"_notify_is_team_for_l2 failed: {_e}")


def _get_entity_owner_id(entity_type: str, name: str, *, owner_id: str = "") -> str:
    """Return string user ID of the entity's creator, empty string if not found.

    created_by is stored as JWT sub (user_id integer as string) or email —
    try integer ID lookup first, fall back to email match.
    """
    try:
        from db.database import SessionLocal
        from db.models import User
        db = SessionLocal()
        try:
            created_by = None
            if entity_type in ("agents", "mcp"):
                from db.models import AgentRecord
                q = db.query(AgentRecord).filter(AgentRecord.name == name)
                if owner_id:
                    q = q.filter(AgentRecord.created_by == owner_id)
                rec = q.first()
                created_by = rec.created_by if rec else None
            elif entity_type == "skills":
                from db.models import SkillRecord
                q = db.query(SkillRecord).filter(SkillRecord.name == name)
                if owner_id:
                    q = q.filter(SkillRecord.created_by == owner_id)
                rec = q.first()
                created_by = rec.created_by if rec else None
            elif entity_type == "workflows":
                from db.models import WorkflowRecord
                q = db.query(WorkflowRecord).filter(WorkflowRecord.name == name)
                if owner_id:
                    q = q.filter(WorkflowRecord.created_by == owner_id)
                rec = q.first()
                created_by = rec.created_by if rec else None
            if created_by:
                # created_by may be an integer ID (from JWT sub) or an email string
                try:
                    u = db.query(User).filter(User.id == int(created_by)).first()
                except (ValueError, TypeError):
                    u = db.query(User).filter(User.email == created_by).first()
                return str(u.id) if u else ""
        finally:
            db.close()
    except Exception:
        pass
    return ""


# ============================================================
# NOTIFICATION HELPER
# ============================================================

def _notify_approvers(entity_type: str, name: str, action: str, actor: str) -> None:
    """Fire-and-forget notification to approvers when submission arrives."""
    try:
        from core.notifications import notify
        notify(
            channel="slack",
            subject=f"[Approval Needed] {entity_type}/{name}",
            message=(
                f"*{name}* ({entity_type}) was submitted for approval by *{actor}*.\n"
                f"Review at: `POST /governance/{entity_type}/{name}/approve`"
            ),
        )
    except Exception as e:
        logger.debug(f"governance_router: notification skipped: {e}")


# ============================================================
# PYDANTIC
# ============================================================

class RejectBody(BaseModel):
    reason: str = ""


# ============================================================
# GUARDS
# ============================================================

def _require_approver(current_user: dict):
    """
    Approve/reject/promote/deprecate requires:
      - admin role, OR
      - APPROVER_ROLES membership (platform_engineer, security), OR
      - ad_level <= int(os.getenv("APPROVAL_AD_LEVEL", "6")) (Director-level and above in the org hierarchy)
    """
    role     = current_user.get("role", "")
    ad_level = int(current_user.get("ad_level", 6) or 6)
    if role not in APPROVER_ROLES and ad_level > int(_os.getenv("APPROVAL_AD_LEVEL", "6")):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Action requires approver privileges "
                f"(role in {sorted(APPROVER_ROLES)} OR ad_level ≤ 3). "
                f"Current role: {role!r}, ad_level: {ad_level}"
            ),
        )


def _require_scoped_approver(entity_type: str, name: str, current_user: dict, owner_id: str = ""):
    """Scope-aware approval guard for approve/reject/promote/deprecate.

    Authorization rules (aligned with the submit-time notification routing —
    "HOD if mapped, else admin only"):
      - ``admin`` role may approve anything (global override).
      - **Public** items (``visibility == "public"``) require **admin only**.
      - **Department-scoped** items (non-public): the **HOD of the creator's
        department** may approve; if that department has **no HOD mapped**,
        approval falls back to **admin only**. We deliberately no longer allow
        any same-department senior to approve — that leaked approval rights to
        seniors who are not the department head.

    HOD identity is taken from the request payload's ``is_hod`` /
    ``hod_departments`` claims — the SAME server-enriched signal the rest of
    the platform uses (auth.rbac.is_hod / get_hod_departments, as in
    budget_router). This matches by the department names the user actually
    heads, which is more reliable than comparing raw user ids across the
    ``sub`` claim vs. ``users.id``. Department comparison is case-insensitive.

    Separation of duties: the maker (the artifact's ``created_by``) may NEVER
    approve their own submission — even if they hold an approver role (admin
    or HOD). This blocks self-approval regardless of scope/role.

    HOD_APPROVAL_ENABLED=false (flat/admin-only mode, default) EXCEPTION:
    flat-mode deployments are assumed to have exactly one admin — there is
    nobody else to review their own agent/skill/MCP/workflow submission, so
    the self-approval block above is skipped for an admin actor only. Any
    non-admin submitter is still blocked (there's no HOD to fall back to in
    flat mode either). HOD_APPROVAL_ENABLED=true keeps the unconditional
    block exactly as before — this exception never applies in that mode.
    """
    # Reference pattern: auth.rbac helpers (already used at list-scoping below
    # and throughout budget_router). is_admin short-circuits HOD by design.
    from auth.rbac import is_admin, is_hod, get_hod_departments

    role      = current_user.get("role", "")
    ad_level  = int(current_user.get("ad_level", 6) or 6)
    user_dept = (current_user.get("department") or "").strip()

    # ── Separation of duties: maker cannot approve their own deploy request.
    # Resolved BEFORE the admin override so an admin who is also the submitter
    # is still blocked. ``_get_entity_owner_id`` returns the creator's User.id
    # as a string (resolving either an integer JWT sub or an email); compare
    # against the approver's id. The JWT payload carries the user id in ``sub``
    # (with ``id`` as a legacy alias), so read both. Falls open (no block) only
    # when the creator can't be resolved — never blocks a legitimate approver
    # on a lookup miss.
    #
    # HOD_APPROVAL_ENABLED=false: the admin is exempted from this block (see
    # docstring) — nobody else exists to approve their own submission.
    approver_id = str(current_user.get("sub") or current_user.get("id", "") or "")
    if approver_id and not (not HOD_APPROVAL_ENABLED and is_admin(current_user)):
        creator_id = _get_entity_owner_id(entity_type, name, owner_id=owner_id)
        if creator_id and creator_id == approver_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    "You cannot approve your own deploy request. "
                    "Separation of duties requires a different approver."
                ),
            )

    # Admin override — can approve anything, any scope, any department.
    if is_admin(current_user):
        return

    visibility = _get_entity_visibility(entity_type, name)  # defaults "public"

    # Public catalog items: admin only (admin already returned above).
    if visibility == "public":
        raise HTTPException(
            status_code=403,
            detail=(
                f"Public items require admin approval. "
                f"Current role: {role!r}, ad_level: {ad_level}"
            ),
        )

    # Department-scoped items: the HOD of the creator's department only.
    creator_dept = (_get_entity_department(entity_type, name) or "").strip()

    # If the department has no HOD mapped, only an admin may approve (admins
    # already returned above), so a non-admin here is rejected outright.
    if not _resolve_hod_user_ids(creator_dept):
        raise HTTPException(
            status_code=403,
            detail=(
                f"No HOD is mapped for department ‘{creator_dept}’; this item "
                f"requires admin approval. Current role: {role!r}"
            ),
        )

    # Otherwise the caller must be an HOD who actually heads the creator's
    # department (case-insensitive match on the department names they own).
    caller_hod_depts = {d.casefold() for d in get_hod_departments(current_user)}
    if not (is_hod(current_user) and creator_dept.casefold() in caller_hod_depts):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Department items require the HOD of department "
                f"‘{creator_dept}’ (or an admin). "
                f"Current department: {user_dept!r}, ad_level: {ad_level}"
            ),
        )


def _actor(current_user: dict) -> str:
    return current_user.get("email") or current_user.get("user_id", "unknown")


# ============================================================
# LIST  (with pagination)
# ============================================================

@router.get("/{entity_type}")
def list_governance(
        entity_type: str,
        skip:   int = Query(0,   ge=0,  description="Number of items to skip"),
        limit:  int = Query(100, ge=1, le=500, description="Max items to return"),
        status: Optional[str] = Query(None, description="Filter by status"),
        current_user: dict = Depends(get_current_user),
):
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown entity type: {entity_type}. "
                                                    f"Valid types: {sorted(ENTITY_TYPES)}")

    items = []

    if entity_type == "agents":
        ab = _get_agent_builder()
        if ab:
            for agent in ab.list_all():
                import dataclasses
                d = dataclasses.asdict(agent)
                items.append({
                    "name":        d["name"],
                    "description": d.get("description", ""),
                    "version":     d.get("version", "1.0.0"),
                    "status":      d.get("status", "PRODUCTION"),
                    "created_by":  d.get("created_by", "platform"),
                    "approved_by": d.get("approved_by", ""),
                    "enabled":     d.get("enabled", True),
                })

    elif entity_type == "skills":
        r = _get_redis()
        if r:
            names = r.smembers(_REDIS_INDICES["skills"]) or set()
            for nm in names:
                raw = r.get(f"{_REDIS_PREFIXES['skills']}{nm}")
                if raw:
                    try:
                        d = json.loads(raw)
                        items.append({
                            "name":        d.get("name", nm),
                            "description": d.get("description", ""),
                            "version":     d.get("version", "1.0.0"),
                            "status":      d.get("status", "PRODUCTION"),
                            "created_by":  d.get("created_by", "platform"),
                            "approved_by": d.get("approved_by", ""),
                        })
                    except Exception:
                        pass
        # Also include skills from mcp_registry (built-in)
        registry = _get_mcp_registry()
        if registry:
            existing = {i["name"] for i in items}
            for s in registry.skills.list_all():
                if s.name not in existing:
                    items.append({
                        "name":        s.name,
                        "description": s.description,
                        "version":     getattr(s, "version", "1.0.0"),
                        "status":      getattr(s, "status", "PRODUCTION"),
                        "created_by":  getattr(s, "author", "platform"),
                        "approved_by": "",
                    })

    elif entity_type == "mcp":
        registry = _get_mcp_registry()
        if registry:
            for t in registry.tools.list_all():
                items.append({
                    "name":        t.name,
                    "description": t.description,
                    "status":      getattr(t, "status", "PRODUCTION"),
                    "created_by":  getattr(t, "created_by", "platform"),
                    "approved_by": getattr(t, "approved_by", ""),
                    "tags":        t.tags,
                    "enabled":     t.enabled,
                })

    elif entity_type == "workflows":
        # Read from Redis workflow store
        r = _get_redis()
        if r:
            names = r.smembers(_REDIS_INDICES["workflows"]) or set()
            for nm in names:
                raw = r.get(f"{_REDIS_PREFIXES['workflows']}{nm}")
                if raw:
                    try:
                        d = json.loads(raw)
                        items.append({
                            "name":        d.get("name", nm),
                            "description": d.get("description", ""),
                            "status":      d.get("status", "PRODUCTION"),
                            "created_by":  d.get("created_by", "platform"),
                            "approved_by": d.get("approved_by", ""),
                        })
                    except Exception:
                        pass

    # Visibility + department scoping (agents and workflows only)
    # Admins see everything; others see: own items, OR public+PRODUCTION, OR private+same dept
    from auth.rbac import is_admin as _gov_is_admin
    if entity_type in ("agents", "workflows") and not _gov_is_admin(current_user):
        _uid  = current_user.get("sub") or current_user.get("id", "")
        _uemail = current_user.get("email", "")
        _dept = current_user.get("department", "")
        filtered = []
        for item in items:
            vis        = item.get("visibility")       # None = legacy, treat as public
            item_dept  = item.get("department", "")
            created_by = item.get("created_by", "")
            item_status = item.get("status", "")
            is_legacy    = vis is None                # pre-RBAC record — visible to all
            is_own       = created_by and (created_by == _uid or created_by == _uemail)
            is_pub_prod  = vis == "public"  and item_status in ("APPROVED", "PRODUCTION")
            is_priv_dept = vis == "private" and item_dept == _dept
            if is_legacy or is_own or is_pub_prod or is_priv_dept:
                filtered.append(item)
        items = filtered

    # Filter by status
    if status:
        items = [i for i in items if i.get("status", "").upper() == status.upper()]

    # Pagination
    total = len(items)
    items = items[skip: skip + limit]

    return {
        "entity_type": entity_type,
        "total":       total,
        "skip":        skip,
        "limit":       limit,
        "items":       items,
    }


# ============================================================
# ENTITY GRAPH PREVIEW — GET /{entity_type}/{name}/graph
# Approver-only. Lets an admin/HOD see the full submitted workflow (nodes +
# edges) inside the Inbox approval panel. The normal ABStudio read endpoint is
# owner-scoped (WHERE owner_user_id = current_user), so an approver — a
# different user than the submitter — would get a 404. This endpoint requires
# approver privileges and reads by (name, owner_id) instead.
# ============================================================

@router.get("/{entity_type}/{name}/graph")
def get_entity_graph(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID of the submitter"),
        current_user: dict = Depends(get_current_user),
):
    """Return a submitted workflow's graph for approver preview in the Inbox."""
    if entity_type != "workflows":
        raise HTTPException(status_code=404,
                            detail="graph preview is only available for workflows")
    _require_approver(current_user)
    try:
        import asyncio as _asyncio
        from app.core import workflow_repo as _wr
        wf = _asyncio.run(_wr.get_workflow_by_name(name, owner_id or ""))
    except Exception as e:
        logger.warning("governance: graph preview lookup failed for %s/%s: %s",
                       entity_type, name, e)
        raise HTTPException(status_code=500, detail="failed to load workflow graph")
    if not wf:
        raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
    graph = wf.get("graphData") or {}
    return {
        "name":        wf.get("name"),
        "description": wf.get("description"),
        "author":      wf.get("author"),
        "graphData":   {"nodes": graph.get("nodes", []),
                        "edges": graph.get("edges", [])},
    }


# ============================================================
# STANDALONE AGENT CONFIG PREVIEW — GET /{entity_type}/{name}/config
# Approver-only. Lets an admin/HOD inspect a submitted Agent Builder agent's
# config (system prompt + tools + skills + model) from the Inbox approval panel.
# Reads the governance mirror (agents_pg = AgentRecord) directly, scoped by
# (name, owner_id) so an approver only ever sees the specific submitter's agent.
# ============================================================

@router.get("/{entity_type}/{name}/config")
def get_entity_config(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID of the submitter"),
        current_user: dict = Depends(get_current_user),
):
    """Return a submitted standalone agent's config for approver preview.

    The full agent config (instructions, tools, skills) lives in ABStudio's own
    ``agents`` table — ``agents_pg`` (AgentRecord) is only the governance status
    mirror and does not carry the prompt/tools. So, like the workflow graph
    preview, we read from ABStudio's repo by (name, owner_id).
    """
    if entity_type != "agents":
        raise HTTPException(status_code=404,
                            detail="config preview is only available for agents")
    _require_approver(current_user)
    try:
        import asyncio as _asyncio
        from app.core import workflow_repo as _wr
        agent = _asyncio.run(_wr.get_agent_by_name(name, owner_id or ""))
    except Exception as e:
        logger.warning("governance: config preview lookup failed for %s/%s: %s",
                       entity_type, name, e)
        raise HTTPException(status_code=500, detail="failed to load agent config")
    if not agent:
        raise HTTPException(status_code=404, detail=f"agent {name!r} not found")
    return {
        "name":         agent.get("name"),
        "description":  agent.get("description"),
        "instructions": agent.get("instructions"),
        "tools":        agent.get("tools") or [],
        "skills":       agent.get("skills") or [],
    }


# ============================================================
# SKILL SOURCE PREVIEW — GET /{entity_type}/{name}/source
# Approver-only. Lets an admin/HOD inspect a submitted skill's code + schemas +
# permissions from the Inbox approval panel. Reads the governance mirror
# (skills_pg = SkillRecord) directly, scoped by (name, owner_id) so an approver
# only ever sees the specific submitter's skill.
# ============================================================

@router.get("/{entity_type}/{name}/source")
def get_entity_source(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID of the submitter"),
        current_user: dict = Depends(get_current_user),
):
    """Return a submitted skill's source for approver preview.

    The skill body lives in ABStudio's global ``skills_catalog`` table (keyed by
    name), not in ``skills_pg`` (which is only the governance status mirror and
    carries no code). So we read via the ABStudio repo by name. ``owner_id`` is
    accepted for URL symmetry but skills_catalog is a shared, name-keyed catalog.
    """
    if entity_type != "skills":
        raise HTTPException(status_code=404,
                            detail="source preview is only available for skills")
    _require_approver(current_user)
    try:
        import asyncio as _asyncio
        from app.core import workflow_repo as _wr
        skill = _asyncio.run(_wr.get_skill(name))
    except Exception as e:
        logger.warning("governance: source preview lookup failed for %s/%s: %s",
                       entity_type, name, e)
        raise HTTPException(status_code=500, detail="failed to load skill source")
    if not skill:
        raise HTTPException(status_code=404, detail=f"skill {name!r} not found")
    return {
        "name":        skill.get("name"),
        "description": skill.get("description"),
        "category":    skill.get("category"),
        "code":        skill.get("content"),
        "generated":   skill.get("generated"),
    }


# ============================================================
# SINGLE ENTITY STATUS — GET /{entity_type}/{name}
# Called by Inbox.jsx on item select to get the live governance status.
# ============================================================

@router.get("/{entity_type}/{name}")
def get_entity_status(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID for owner-scoped status"),
        current_user: dict = Depends(get_current_user),
):
    """Return the current governance status for a single entity (agents/skills/mcp/workflows)."""
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown entity type: {entity_type}")
    status = _get_entity_status(entity_type, name, owner_id=owner_id or "")
    if status is None:
        raise HTTPException(status_code=404, detail=f"{entity_type}/{name} not found")
    return {"name": entity_type, "entity_name": name, "status": status}


# ============================================================
# GENERIC TRANSITION HANDLER
# ============================================================

def _transition(
        entity_type: str,
        name: str,
        action: str,
        actor: str,
        extra_updates: dict = None,
        reason: Optional[str] = None,
        *,
        owner_id: str = "",
):
    """Apply a governance state transition with full validation."""
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown entity type: {entity_type}")

    current_status = _get_entity_status(entity_type, name, owner_id=owner_id)
    if current_status is None:
        raise HTTPException(status_code=404, detail=f"{entity_type}/{name} not found")

    valid_from, to_status = _VALID_TRANSITIONS[action]
    if current_status not in valid_from:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} '{name}': current status is {current_status!r}. "
                   f"Required: one of {list(valid_from)}",
        )

    # Store a tz-aware UTC ISO string so Postgres records the correct absolute
    # moment on TIMESTAMPTZ columns. A naive ``.utcnow().isoformat()`` string
    # is interpreted by Postgres in the session TimeZone (Asia/Kolkata on this
    # host) and would shift the stored moment by −5:30h — the frontend then
    # renders the approval time as "5:27 AM" for a 10:57 AM IST event.
    updates = {"status": to_status, f"{action}_by": actor,
               f"{action}_at": datetime.now(timezone.utc).isoformat()}
    if extra_updates:
        updates.update(extra_updates)
    if reason is not None:
        updates["rejection_reason"] = reason

    _set_entity_governance(entity_type, name, updates, owner_id=owner_id)
    # Audit log write is fire-and-forget — never block the API response
    _bg(_pg_record_event, entity_type, name, action, actor, current_status, to_status, reason, owner_id=owner_id)
    logger.info(f"Governance: {action} {entity_type}/{name} by {actor}: {current_status} → {to_status}")

    return current_status, to_status


# ============================================================
# SUBMIT
# ============================================================

@router.post("/{entity_type}/{name}/submit")
def submit_for_approval(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID for owner-scoped submit"),
        current_user: dict = Depends(get_current_user),
):
    actor = _actor(current_user)
    from_status, to_status = _transition(entity_type, name, "submit", actor, owner_id=owner_id or "")
    # Sync PENDING_APPROVAL status to marketplace KV for MCP tools
    if entity_type == "mcp":
        _sync_marketplace_status(name, to_status)
    # Notify approvers via Slack and Inbox
    _notify_approvers(entity_type, name, "submit", actor)
    _bg(_governance_notify, entity_type, name, "submit", from_status, to_status, actor, owner_id=owner_id or "")
    # Notify admins via inbox
    try:
        from store.inbox_store import publish_inbox_item as _pub
        from db.database import SessionLocal as _GovDB
        from db.models import User as _GovUser
        _gdb = _GovDB()
        try:
            _admins = _gdb.query(_GovUser).filter(_GovUser.role == "admin").all()
            for _admin in _admins:
                _pub(
                    user_id=str(_admin.id),
                    type="governance_approval_needed",
                    title=f"Approval needed: {entity_type} '{name}'",
                    body=f"{current_user.get('email', 'A user')} submitted '{name}' for approval.",
                    source_id=name,
                    metadata={"entity_type": entity_type, "name": name, "submitted_by": current_user.get("email", "unknown")},
                )
        finally:
            _gdb.close()
    except Exception as _notify_err:
        logger.debug(f"Governance notify failed (non-critical): {_notify_err}")
    return {"status": to_status, "submitted_by": actor,
            "message": f"Submitted for approval. Approvers have been notified."}


# ============================================================
# APPROVE
# ============================================================

@router.post("/{entity_type}/{name}/approve")
def approve_entity(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID for owner-scoped approve"),
        current_user: dict = Depends(get_current_user),
):
    actor       = _actor(current_user)
    ad_level    = int(current_user.get("ad_level", 6) or 6)
    role        = current_user.get("role", "")
    dept        = (current_user.get("department") or "").strip()
    is_l1       = role in APPROVER_ROLES or ad_level <= int(_os.getenv("APPROVAL_AD_LEVEL", "6"))
    is_l2_is    = dept in _IS_TEAM_DEPTS or role == "admin"
    _oid        = owner_id or ""

    current_status = _get_entity_status(entity_type, name, owner_id=_oid)
    if current_status is None:
        raise HTTPException(status_code=404, detail=f"{entity_type}/{name} not found")

    # ── Two-level approval for critical MCP tools ─────────────
    if entity_type == "mcp" and current_status == "PENDING_APPROVAL":
        # Check if tool is marked critical
        _r2 = _get_marketplace_kv()
        is_critical = False
        if _r2 is not None:
            raw = _r2.get(f"marketplace:tool:{name}")
            if raw:
                try:
                    is_critical = json.loads(raw).get("is_critical", False)
                except Exception:
                    pass
        if is_critical:
            # L1 approver moves to PENDING_L2; IS team approves L2
            if not is_l1:
                raise HTTPException(status_code=403, detail="L1 approval requires ad_level ≤ 3 or admin/approver role.")
            # Transition PENDING_APPROVAL → PENDING_L2
            updates = {"status": "PENDING_L2", "l1_approved_by": actor,
                       "l1_approved_at": datetime.now(timezone.utc).isoformat()}
            _set_entity_governance(entity_type, name, updates, owner_id=_oid)
            _sync_marketplace_status(name, "PENDING_L2")
            _bg(_pg_record_event, entity_type, name, "approve_l1", current_status, "PENDING_L2", None, owner_id=_oid)
            _bg(_notify_is_team_for_l2, entity_type, name, actor)
            logger.info(f"Governance: approve_l1 mcp/{name} by {actor}: PENDING_APPROVAL → PENDING_L2")
            return {"status": "PENDING_L2", "l1_approved_by": actor,
                    "message": "L1 approved. Awaiting IS/Security team (L2) approval."}

    if current_status == "PENDING_L2":
        # IS team L2 approval
        if not is_l2_is:
            raise HTTPException(
                status_code=403,
                detail=f"L2 approval requires IS/AppSec/InfoSec team membership (IS_TEAM_DEPARTMENTS: {sorted(_IS_TEAM_DEPTS)}) or admin role."
            )

    # Standard approval (non-critical or PENDING_L2 → APPROVED)
    _require_scoped_approver(entity_type, name, current_user, owner_id=owner_id or "")
    from_status, to_status = _transition(
        entity_type, name, "approve", actor,
        extra_updates={"approved_by": actor,
                       "approved_at": datetime.now(timezone.utc).isoformat()},
        owner_id=_oid,
    )
    # Activate agent-scoped KB docs when an agent is approved.
    # Each linked KnowledgeDocument is embedded into pgvector using
    # repo='agent_kb:{name}' so it becomes immediately RAG-searchable.
    if entity_type == "agents":
        def _activate_agent_kb_docs(agent_name: str, approver: str):
            try:
                from db.database import SessionLocal as _KBSession
                from db.models import AgentKbDoc as _AKD
                from store.docs_store import activate_doc as _activate_doc
                _kbdb = _KBSession()
                try:
                    links = _kbdb.query(_AKD).filter(_AKD.agent_id == agent_name).all()
                    # Normalize: spaces/hyphens → underscores, lowercase — must match AgentRunner._run_tools()
                    _agent_kb_repo = f"agent_kb:{agent_name.strip().lower().replace(' ', '_').replace('-', '_')}"
                    for link in links:
                        try:
                            _activate_doc(
                                doc_id=str(link.doc_id),
                                approved_by=approver,
                                repo=_agent_kb_repo,
                            )
                            logger.info(f"governance: activated agent KB doc {link.doc_id} → repo={_agent_kb_repo!r}")
                        except Exception as _doc_err:
                            logger.warning(f"governance: failed to activate agent KB doc {link.doc_id}: {_doc_err}")
                finally:
                    _kbdb.close()
            except Exception as _kb_err:
                logger.warning(f"governance: _activate_agent_kb_docs failed: {_kb_err}")
        _bg(_activate_agent_kb_docs, name, actor)

    # Publish-on-approval: a "Deploy" request for a Build Studio workflow/agent
    # becomes a shared catalog template once the HOD approves. Runs off the
    # approval path in a background thread. See _publish_as_template.
    if entity_type in ("workflows", "agents"):
        _bg(_publish_as_template, entity_type, name, owner_id=_oid)

    # Sync APPROVED status back to the marketplace KV (DB=3) for MCP tools
    if entity_type == "mcp":
        _sync_marketplace_status(name, to_status)
    _bg(_governance_notify, entity_type, name, "approve", from_status, to_status, actor, owner_id=_oid)
    return {"status": to_status, "approved_by": actor}


# ============================================================
# REJECT
# ============================================================

@router.post("/{entity_type}/{name}/reject")
def reject_entity(
        entity_type: str,
        name: str,
        body: RejectBody = RejectBody(),
        owner_id: Optional[str] = Query(None, description="Owner user ID for owner-scoped reject"),
        current_user: dict = Depends(get_current_user),
):
    _require_scoped_approver(entity_type, name, current_user, owner_id=owner_id or "")

    _ok_name, _errs_name, _san_name = validate_identifier(name)
    if not _ok_name:
        raise HTTPException(status_code=400, detail=_flatten_errors({"name": _errs_name}))
    name = _san_name

    _ok_reason, _errs_reason, _san_reason = validate_free_text(body.reason or "")
    if not _ok_reason:
        raise HTTPException(status_code=400, detail=_flatten_errors({"reason": _errs_reason}))
    body.reason = _san_reason

    actor = _actor(current_user)
    _oid = owner_id or ""
    from_status, to_status = _transition(
        entity_type, name, "reject", actor,
        extra_updates={"rejected_by": actor},
        reason=body.reason or None,
        owner_id=_oid,
    )
    # Sync REJECTED status back to the marketplace KV for MCP tools
    if entity_type == "mcp":
        _sync_marketplace_status(name, to_status)
    _bg(_governance_notify, entity_type, name, "reject", from_status, to_status, actor, body.reason or None, owner_id=_oid)
    return {"status": to_status, "rejected_by": actor, "reason": body.reason,
            "note": "Entity can be re-submitted after fixes via POST .../submit"}


# ============================================================
# WITHDRAW  (owner cancels a pending deploy request)
# ============================================================

@router.post("/{entity_type}/{name}/withdraw")
def withdraw_entity(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID for owner-scoped withdraw"),
        current_user: dict = Depends(get_current_user),
):
    """Cancel a pending approval request and return the artifact to DRAFT.

    Unlike approve/reject (approver-only), a withdraw is initiated by the
    submitter: the person who deployed the artifact may cancel while it is
    still awaiting approval. Admins/approvers may also withdraw on the
    owner's behalf. After a withdraw the artifact is editable again and can be
    re-submitted via POST .../submit.
    """
    actor = _actor(current_user)
    _oid = owner_id or ""
    caller_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    is_approver = (current_user.get("role") in APPROVER_ROLES)
    # Owner-or-approver: the submitter (owner) can cancel their own request;
    # admins/approvers can cancel on their behalf. When no owner scope is given
    # (platform-level artifact) fall back to approver-only.
    if not is_approver and _oid and caller_id and caller_id != _oid:
        raise HTTPException(
            status_code=403,
            detail="Only the submitter or an approver can cancel this request.",
        )
    from_status, to_status = _transition(
        entity_type, name, "withdraw", actor,
        extra_updates={"withdrawn_by": actor},
        owner_id=_oid,
    )
    if entity_type == "mcp":
        _sync_marketplace_status(name, to_status)
    _bg(_governance_notify, entity_type, name, "withdraw", from_status, to_status, actor, owner_id=_oid)
    return {"status": to_status, "withdrawn_by": actor,
            "note": "Deploy request cancelled. The artifact is editable again and can be re-submitted."}


# ============================================================
# PROMOTE TO PRODUCTION
# ============================================================

@router.post("/{entity_type}/{name}/promote")
def promote_entity(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID for owner-scoped promote"),
        current_user: dict = Depends(get_current_user),
):
    _require_scoped_approver(entity_type, name, current_user, owner_id=owner_id or "")
    actor = _actor(current_user)
    _oid = owner_id or ""
    from_status, to_status = _transition(
        entity_type, name, "promote", actor,
        extra_updates={"is_production": True, "promoted_by": actor},
        owner_id=_oid,
    )
    # Hot-reload the in-memory agent cache from Postgres so the new/updated
    # system_prompt takes effect immediately without a restart.
    if entity_type == "agents":
        ab = _get_agent_builder()
        if ab and hasattr(ab, "reload_from_db"):
            _bg(ab.reload_from_db, name)
    _bg(_governance_notify, entity_type, name, "promote", from_status, to_status, actor, owner_id=_oid)
    return {"status": to_status, "promoted_by": actor}


# ============================================================
# DEPRECATE
# ============================================================

@router.post("/{entity_type}/{name}/deprecate")
def deprecate_entity(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID for owner-scoped deprecate"),
        current_user: dict = Depends(get_current_user),
):
    _require_scoped_approver(entity_type, name, current_user, owner_id=owner_id or "")
    actor = _actor(current_user)
    _oid = owner_id or ""
    from_status, to_status = _transition(
        entity_type, name, "deprecate", actor,
        extra_updates={"is_production": False, "deprecated_by": actor},
        owner_id=_oid,
    )
    _bg(_governance_notify, entity_type, name, "deprecate", from_status, to_status, actor, owner_id=_oid)
    return {"status": to_status, "deprecated_by": actor,
            "warning": f"{entity_type}/{name} is now DEPRECATED and cannot be executed."}


# ============================================================
# SLA REMINDER — 5-day auto-reminder for stale PENDING_APPROVAL items
# ============================================================

def check_governance_sla_reminders() -> dict:
    """
    Query governance_events for items stuck in PENDING_APPROVAL for > 5 days
    with no subsequent approve/reject event.  For each stale item:
      1. Push an inbox notification to all approver-role users.
      2. Fire a Slack/Teams notification via core.notifications.notify.
    Returns a summary dict with the count of overdue items processed.
    Called by the cron scheduler daily at 09:00 IST (03:30 UTC).
    """
    _SLA_DAYS = 5
    _SQL = """
        SELECT DISTINCT ON (name, entity_type)
               name, entity_type, to_status, created_at
        FROM   governance_events
        WHERE  to_status = 'PENDING_APPROVAL'
          AND  created_at < NOW() - INTERVAL ':sla_days days'
          AND  (name, entity_type) NOT IN (
                   SELECT name, entity_type
                   FROM   governance_events
                   WHERE  action IN ('approve', 'reject')
                     AND  created_at > NOW() - INTERVAL '30 days'
               )
        ORDER  BY name, entity_type, created_at DESC
    """.replace(":sla_days", str(_SLA_DAYS))

    overdue = []
    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            rows = db.execute(text(_SQL)).fetchall()
            for row in rows:
                overdue.append({
                    "name":        row[0],
                    "entity_type": row[1],
                    "status":      row[2],
                    "submitted_at": row[3].isoformat() if row[3] else None,
                })
        finally:
            db.close()
    except Exception as e:
        logger.error(f"check_governance_sla_reminders: DB query failed: {e}")
        return {"overdue_count": 0, "error": str(e)}

    if not overdue:
        logger.info("check_governance_sla_reminders: no overdue items found")
        return {"overdue_count": 0, "items": []}


    notified = 0
    for item in overdue:
        name        = item["name"]
        entity_type = item["entity_type"]
        submitted   = item["submitted_at"] or "unknown date"

        # Resolve recipients PER ITEM with the same visibility-aware rule as
        # the submit notification and the approval guard: public -> admins,
        # private -> mapped HOD else admins. Never the broad ad_level ≤ 3
        # senior set — that spilled reminders across departments.
        _item_dept  = _get_entity_department(entity_type, name)
        _item_vis   = _get_entity_visibility(entity_type, name)
        approver_user_ids = _resolve_approval_recipients(_item_dept, _item_vis)

        msg_body = (
            f"**SLA OVERDUE** — `{entity_type}/{name}` has been awaiting approval "
            f"for more than {_SLA_DAYS} days (submitted: {submitted}). "
            f"Please review at `POST /governance/{entity_type}/{name}/approve` or "
            f"`POST /governance/{entity_type}/{name}/reject`."
        )

        # 1. Inbox notification to every approver
        for uid in approver_user_ids:
            try:
                from store.inbox_store import publish_inbox_item
                publish_inbox_item(
                    user_id  = uid,
                    type     = "governance_approval",
                    title    = f"[SLA Overdue] {entity_type}/{name} awaiting approval",
                    body     = msg_body,
                    source_id= name,
                    metadata = {
                        "entity_type":  entity_type,
                        "entity_name":  name,
                        "status":       "PENDING_APPROVAL",
                        "submitted_at": submitted,
                        "sla_days":     _SLA_DAYS,
                    },
                )
            except Exception as e:
                logger.warning(f"check_governance_sla_reminders: inbox push failed for uid={uid}: {e}")

        # 2. Slack/Teams channel notification
        try:
            from core.notifications import notify
            notify(
                channel = "slack",
                subject = f"[SLA Overdue] Governance approval needed: {entity_type}/{name}",
                message = (
                    f":warning: *SLA Overdue* — `{entity_type}/{name}` has been in "
                    f"*PENDING_APPROVAL* for >{_SLA_DAYS} days (submitted: {submitted}).\n"
                    f"Reviewers: approve or reject via "
                    f"`POST /governance/{entity_type}/{name}/approve`"
                ),
            )
        except Exception as e:
            logger.debug(f"check_governance_sla_reminders: Slack notify skipped for {name}: {e}")

        notified += 1
        logger.info(
            f"check_governance_sla_reminders: reminder sent for {entity_type}/{name} "
            f"(submitted {submitted})"
        )

    logger.info(f"check_governance_sla_reminders: {notified}/{len(overdue)} reminders sent")
    return {"overdue_count": len(overdue), "notified": notified, "items": overdue}


# ── SLA query helper (shared between endpoint + cron) ────────────────────────

def _query_overdue_items() -> list:
    """Return raw list of overdue PENDING_APPROVAL items from Postgres."""
    _SLA_DAYS = 5
    _SQL = """
        SELECT DISTINCT ON (name, entity_type)
               name, entity_type, to_status, created_at
        FROM   governance_events
        WHERE  to_status = 'PENDING_APPROVAL'
          AND  created_at < NOW() - INTERVAL ':sla_days days'
          AND  (name, entity_type) NOT IN (
                   SELECT name, entity_type
                   FROM   governance_events
                   WHERE  action IN ('approve', 'reject')
                     AND  created_at > NOW() - INTERVAL '30 days'
               )
        ORDER  BY name, entity_type, created_at DESC
    """.replace(":sla_days", str(_SLA_DAYS))
    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            rows = db.execute(text(_SQL)).fetchall()
            return [
                {
                    "name":         row[0],
                    "entity_type":  row[1],
                    "status":       row[2],
                    "submitted_at": row[3].isoformat() if row[3] else None,
                    "sla_days":     _SLA_DAYS,
                }
                for row in rows
            ]
        finally:
            db.close()
    except Exception as e:
        logger.error(f"_query_overdue_items: {e}")
        return []


# ── GET /governance/sla/overdue ───────────────────────────────────────────────

@router.get("/sla/overdue")
def get_sla_overdue(
        current_user: dict = Depends(get_current_user),
):
    """
    Return all PENDING_APPROVAL items that have exceeded the 5-day SLA.
    Accessible by admin and operator roles.
    """
    role = current_user.get("role", "")
    if role not in {*APPROVER_ROLES, "operator"}:
        raise HTTPException(
            status_code=403,
            detail="Requires admin, operator, platform_engineer, or security role.",
        )
    items = _query_overdue_items()
    return {
        "sla_days":     5,
        "overdue_count": len(items),
        "items":        items,
    }


# ── POST /governance/sla/remind ──────────────────────────────────────────────

@router.post("/sla/remind")
def trigger_sla_reminders(
        current_user: dict = Depends(get_current_user),
):
    """
    Manually trigger SLA reminder notifications for all overdue items.
    Admin only.
    """
    role = current_user.get("role", "")
    if role != "admin":
        raise HTTPException(status_code=403, detail="Requires admin role.")
    actor = _actor(current_user)
    logger.info(f"Manual SLA reminder triggered by {actor}")
    result = check_governance_sla_reminders()
    return {
        "triggered_by": actor,
        **result,
    }


# ============================================================
# HISTORY  (always returns list, never 404)
# ============================================================

@router.get("/{entity_type}/{name}/history")
def get_history(
        entity_type: str,
        name: str,
        owner_id: Optional[str] = Query(None, description="Owner user ID for owner-scoped history"),
        current_user: dict = Depends(get_current_user),
):
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown entity type: {entity_type}")

    # Primary: Postgres (durable, survives restarts)
    history = _pg_get_history(entity_type, name, owner_id=owner_id or "")

    return {
        "entity_type": entity_type,
        "name":        name,
        "count":       len(history),
        "history":     history,   # always a list — empty if no events yet
    }
