# SPDX-License-Identifier: MIT
# ============================================================
# INBOX ROUTER — /inbox
# ============================================================

import json
import logging
import time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from auth.dependencies import get_current_user
# Shared UTC-safe timestamp converter — Python's naive ``.timestamp()`` uses the
# server's LOCAL zone, which corrupts naive-UTC values by the local offset. All
# rows returned to the browser must go through ``_utc_posix``.
from store.inbox_store import _utc_posix

logger = logging.getLogger(__name__)

router = APIRouter(tags=["inbox"])


@router.get("/inbox")
def get_inbox(
        user: str = Query(...),
        type: Optional[str] = Query(default=None),
        limit: int = Query(default=50),
        current_user: dict = Depends(get_current_user),
):
    # SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    # This endpoint previously trusted the caller-supplied ``user`` query
    # param as-is with no verification, so any caller could read ANY other
    # user's inbox (approval requests, notifications, etc.) just by passing
    # a different id.
    # Fix: added `current_user: dict = Depends(get_current_user)` as a
    # function parameter (enforces a valid JWT) and, on the next line,
    # overwrite the `user` variable with the id from the verified token
    # (current_user["sub"]) before it is passed to the store functions
    # below. The `user` query param is still accepted on the request (so
    # existing callers don't need a request-shape change) but its value is
    # now discarded for identity purposes.
    user = current_user.get("sub") or current_user.get("user_id") or user
    from store.inbox_store import get_inbox, unread_count
    items = get_inbox(user_id=user, type_filter=type, limit=limit)
    count = unread_count(user)
    return {"items": items, "unread_count": count}


@router.post("/inbox/{item_id}/read")
def mark_item_read(item_id: str):
    from store.inbox_store import mark_read
    ok = mark_read(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"success": True}


@router.post("/inbox/read-all")
def mark_all_read(
        user: str = Query(...),
        current_user: dict = Depends(get_current_user),
):
    # SECURITY (AppSec finding — Information Disclosure / CWE-306): previously
    # trusted the ``user`` query param verbatim, letting any caller mark
    # (and thus discover the existence/count of) another user's inbox items
    # as read.
    # Fix: added `current_user: dict = Depends(get_current_user)` and
    # reassigned `user` from `current_user["sub"]` below before it's used,
    # discarding the caller-supplied query-param value.
    user = current_user.get("sub") or current_user.get("user_id") or user
    from store.inbox_store import mark_all_read
    count = mark_all_read(user)
    return {"success": True, "marked": count}


@router.delete("/inbox/{item_id}")
def delete_item(item_id: str, user: str = Query(...)):
    from store.inbox_store import delete_item
    ok = delete_item(user, item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"success": True}


@router.get("/inbox/unread-count")
def get_unread_count(
        user: str = Query(...),
        current_user: dict = Depends(get_current_user),
):
    # SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    # previously trusted the ``user`` query param verbatim, letting any
    # caller read another user's unread-item count.
    # Fix: added `current_user: dict = Depends(get_current_user)` and
    # reassigned `user` from `current_user["sub"]` below before it's used,
    # discarding the caller-supplied query-param value.
    user = current_user.get("sub") or current_user.get("user_id") or user
    from store.inbox_store import unread_count
    return {"user": user, "unread_count": unread_count(user)}


def _pending_body(label: str, name: str, department: Optional[str],
                  visibility: Optional[str], description: Optional[str]) -> str:
    """Body for an approver-facing ``[Needs Approval]`` inbox item.

    Uses the SAME ``Sent to:`` wording as the maker's ``[Submitted]`` item and
    the submit-button confirmation (all three resolve approver names via
    ``_resolve_approver_display_names``), so a maker and an approver looking at
    their respective inboxes see a consistent description of who the request
    went to. Falls back to a generic phrase when the resolver returns nothing.
    """
    who = "your department manager / admins"
    try:
        from routers.governance_router import _resolve_approver_display_names
        names = _resolve_approver_display_names(department or "", visibility or "private")
        if names:
            who = ", ".join(names)
    except Exception:
        pass
    # Scope line so the maker's own [Submitted] inbox row shows the same
    # Public/Department cue that the approver's [Needs Approval] row shows.
    scope_label = "Public"
    if (visibility or "private").strip().lower() != "public":
        scope_label = "Department" + (f" ({department})" if department else "")
    return (
        f"**{label} `{name}`** is awaiting approval.\n\n"
        f"**Scope:** {scope_label}\n"
        f"**Sent to:** {who}"
        + (f"\n\n{description}" if description else "")
    )


def _latest_submit_ts(entity_type: str, names: list) -> dict:
    """For each entity name, return the ``created_at`` of its most recent
    ``submit`` governance event as a POSIX timestamp.

    Bug fix: when a rejected artifact is re-submitted, ``skills_pg.created_at``
    still points at the ORIGINAL insert time, so the live pending-approvals
    row appeared "stale" (buried in the list). The authoritative "submitted at"
    time is the newest ``governance_events`` row with ``action='submit'``.

    Returns ``{name: posix_ts}``. Missing entries mean no submit event was
    found — callers should fall back to the mirror row's ``created_at``.
    Best-effort: a query error yields an empty dict so callers keep the old
    timestamp instead of erroring the whole endpoint.
    """
    if not names:
        return {}
    try:
        from db.database import SessionLocal
        from db.models import GovernanceEvent
        db = SessionLocal()
        try:
            rows = (
                db.query(GovernanceEvent.name, GovernanceEvent.created_at)
                .filter(
                    GovernanceEvent.entity_type == entity_type,
                    GovernanceEvent.name.in_(list(names)),
                    GovernanceEvent.action == "submit",
                )
                .order_by(GovernanceEvent.created_at.desc())
                .all()
            )
        finally:
            db.close()
        # First occurrence (already sorted desc) wins per name.
        from store.inbox_store import _utc_posix
        latest: dict = {}
        for name, ts in rows:
            if name in latest or not ts:
                continue
            latest[name] = _utc_posix(ts)
        return latest
    except Exception:
        logger.debug("_latest_submit_ts(%s) failed", entity_type)
        return {}


def _resolve_owner_emails(created_by_values: list) -> dict:
    """Batch-resolve a set of ``created_by`` tokens to ``{token: email}``.

    ``created_by`` is stored as either a user-id string or an email. We look up
    the ones that aren't already emails against ``users.email`` (by id) so the
    frontend can do a maker-check (``me.email === meta.submitted_by``) on live
    pending-approval items. Returns only the resolvable mappings; missing keys
    are simply absent. Kept cheap (one query) so the live-items loop stays N+0.
    """
    out: dict = {}
    if not created_by_values:
        return out
    # Emails pass through unchanged; everything else is treated as a user id.
    ids_to_lookup = []
    for v in created_by_values:
        if not v:
            continue
        s = str(v)
        if "@" in s:
            out[s] = s
        else:
            ids_to_lookup.append(s)
    if not ids_to_lookup:
        return out
    try:
        from db.database import SessionLocal
        from db.models import User
        db = SessionLocal()
        try:
            rows = db.query(User.id, User.email).filter(User.id.in_(ids_to_lookup)).all()
            for uid, email in rows:
                if email:
                    out[str(uid)] = email
        finally:
            db.close()
    except Exception:
        pass
    return out


@router.get("/inbox/pending-approvals")
def get_pending_approvals(
        user: str = Query(...),
        current_user: dict = Depends(get_current_user),
):
    """
    Live query across all entity tables for items in PENDING_APPROVAL state.
    Returns inbox-compatible objects so the UI can merge with notification-driven items.
    Only returned for approvers (ad_level <= int(os.getenv("APPROVAL_AD_LEVEL", "6")) or admin) — verified from JWT.
    """
    import psycopg2
    from core.config import postgres_dsn
    from db.database import SessionLocal
    from db.models import AgentRecord, SkillRecord, WorkflowRecord, KnowledgeDocument

    import os as _inbox_os
    # Check approver status from JWT (authoritative — not from query param)
    ad_level = int(current_user.get("ad_level") or 6)
    role     = current_user.get("role", "")
    dept     = (current_user.get("department") or "").strip()
    _is_team_depts = set(
        d.strip() for d in _inbox_os.environ.get("IS_TEAM_DEPARTMENTS", "IS,AppSec,InfoSec").split(",") if d.strip()
    )
    # HODs (department managers) are approvers for their own department's
    # artifacts even when their ad_level is > 3 — governance now routes ABStudio
    # artifact approvals to the department HOD.
    is_hod         = bool(current_user.get("is_hod", False))
    is_l1_approver = ad_level <= int(_inbox_os.getenv("APPROVAL_AD_LEVEL", "6")) or role == "admin" or is_hod
    is_l2_approver = dept in _is_team_depts or role == "admin"
    if not is_l1_approver and not is_l2_approver:
        return {"items": []}

    db = SessionLocal()
    results = []
    # admins see all departments; L1 approvers see only their own dept (+ NULL = platform-wide)
    is_admin = role == "admin"
    # Department scope for the artifact filters below. An HOD sees the
    # departments they head (hod_departments); everyone else falls back to
    # their own department. NULL-department (platform-wide) rows are always
    # visible to any approver.
    _hod_departments = [d for d in (current_user.get("hod_departments") or []) if d]
    visible_depts = _hod_departments if (is_hod and _hod_departments) else ([dept] if dept else [])

    try:

        # ── Agents ──────────────────────────────────────────────
        try:
            agent_q = db.query(AgentRecord).filter(AgentRecord.status == "PENDING_APPROVAL")
            if not is_admin and visible_depts:
                from sqlalchemy import or_
                agent_q = agent_q.filter(
                    or_(AgentRecord.department.in_(visible_depts), AgentRecord.department.is_(None))
                )
            agent_rows = agent_q.all()
            owner_emails = _resolve_owner_emails([r.created_by for r in agent_rows])
            submit_ts = _latest_submit_ts("agents", [r.name for r in agent_rows])
            for r in agent_rows:
                _mirror_ts = _utc_posix(r.created_at)
                results.append({
                    "id":         f"live-agent-{r.name}",
                    "type":       "governance_approval",
                    "title":      f"[Needs Approval] Agent: {r.name}",
                    "body":       _pending_body("Agent", r.name, r.department, r.visibility, r.description),
                    "source_id":  r.name,
                    "metadata":   {"entity_type": "agents", "entity_name": r.name,
                                   "status": "PENDING_APPROVAL",
                                   "current_status": r.status,
                                   "action": "submit",
                                   "owner_id":     r.created_by or "",
                                   "submitted_by": owner_emails.get(str(r.created_by), ""),
                                   "visibility":   r.visibility or "private",
                                   "department":   r.department or ""},
                    "read":       False,
                    # Prefer the newest ``submit`` event time — on resubmit-after-
                    # reject the mirror row's created_at is stale (original insert).
                    "created_at": submit_ts.get(r.name, _mirror_ts),
                    "live":       True,
                })
        except Exception:
            logger.warning("pending-approvals: agents query failed")

        # ── Skills ──────────────────────────────────────────────
        try:
            skill_q = db.query(SkillRecord).filter(SkillRecord.status == "PENDING_APPROVAL")
            if not is_admin and visible_depts:
                from sqlalchemy import or_
                skill_q = skill_q.filter(
                    or_(SkillRecord.department.in_(visible_depts), SkillRecord.department.is_(None))
                )
            skill_rows = skill_q.all()
            owner_emails = _resolve_owner_emails([r.created_by for r in skill_rows])
            submit_ts = _latest_submit_ts("skills", [r.name for r in skill_rows])
            for r in skill_rows:
                _mirror_ts = _utc_posix(r.created_at)
                results.append({
                    "id":         f"live-skill-{r.name}",
                    "type":       "governance_approval",
                    "title":      f"[Needs Approval] Skill: {r.name}",
                    "body":       _pending_body("Skill", r.name, r.department, r.visibility, r.description),
                    "source_id":  r.name,
                    "metadata":   {"entity_type": "skills", "entity_name": r.name,
                                   "status": "PENDING_APPROVAL",
                                   "current_status": r.status,
                                   "action": "submit",
                                   "owner_id":     r.created_by or "",
                                   "submitted_by": owner_emails.get(str(r.created_by), ""),
                                   "visibility":   r.visibility or "private",
                                   "department":   r.department or ""},
                    "read":       False,
                    # Prefer the newest ``submit`` event time — on resubmit-after-
                    # reject the mirror row's created_at is stale (original insert).
                    "created_at": submit_ts.get(r.name, _mirror_ts),
                    "live":       True,
                })
        except Exception:
            logger.warning("pending-approvals: skills query failed")

        # ── Workflows ───────────────────────────────────────────
        try:
            wf_q = db.query(WorkflowRecord).filter(WorkflowRecord.status == "PENDING_APPROVAL")
            if not is_admin and visible_depts:
                from sqlalchemy import or_
                wf_q = wf_q.filter(
                    or_(WorkflowRecord.department.in_(visible_depts), WorkflowRecord.department.is_(None))
                )
            wf_rows = wf_q.all()
            owner_emails = _resolve_owner_emails([r.created_by for r in wf_rows])
            submit_ts = _latest_submit_ts("workflows", [r.name for r in wf_rows])
            for r in wf_rows:
                _mirror_ts = _utc_posix(r.created_at)
                results.append({
                    "id":         f"live-workflow-{r.name}",
                    "type":       "governance_approval",
                    "title":      f"[Needs Approval] Workflow: {r.name}",
                    "body":       _pending_body("Workflow", r.name, r.department, r.visibility, r.description),
                    "source_id":  r.name,
                    "metadata":   {"entity_type": "workflows", "entity_name": r.name,
                                   "status": "PENDING_APPROVAL",
                                   "current_status": r.status,
                                   "action": "submit",
                                   "owner_id":     r.created_by or "",
                                   "submitted_by": owner_emails.get(str(r.created_by), ""),
                                   "visibility":   r.visibility or "private",
                                   "department":   r.department or ""},
                    "read":       False,
                    # Prefer the newest ``submit`` event time — on resubmit-after-
                    # reject the mirror row's created_at is stale (original insert).
                    "created_at": submit_ts.get(r.name, _mirror_ts),
                    "live":       True,
                })
        except Exception:
            logger.warning("pending-approvals: workflows query failed")

        # ── Products ─────────────────────────────────────────────
        try:
            from db.models import Product, DeptProductMapping
            product_q = db.query(Product).filter(
                Product.status == "PENDING_APPROVAL",
                Product.is_active == True,
                )
            if not is_admin and dept:
                # only show products mapped to this approver's department
                mapped_ids = [
                    row.product_id for row in
                    db.query(DeptProductMapping.product_id)
                    .filter(DeptProductMapping.department == dept)
                    .all()
                ]
                from sqlalchemy import or_
                product_q = product_q.filter(
                    or_(Product.id.in_(mapped_ids), ~db.query(DeptProductMapping)
                        .filter(DeptProductMapping.product_id == Product.id).exists())
                )
            for r in product_q.all():
                results.append({
                    "id":         f"live-product-{r.id}",
                    "type":       "product_approval",
                    "title":      f"[Product] New product pending: {r.name}",
                    "body":       f"**{r.requested_by or r.created_by}** submitted product **{r.name}** for approval.",
                    "source_id":  str(r.id),
                    "metadata":   {"entity_type": "product", "product_id": str(r.id),
                                   "product_name": r.name, "action": "submit"},
                    "read":       False,
                    "created_at": _utc_posix(r.created_at),
                    "live":       True,
                })
        except Exception:
            logger.warning("pending-approvals: products query failed")

        # ── Knowledge Base docs ─────────────────────────────────
        try:
            kb_q = db.query(KnowledgeDocument).filter(KnowledgeDocument.status == "PENDING_APPROVAL")
            if not is_admin and dept:
                # department_ids JSONB array: [] = platform-wide; non-empty = dept-scoped.
                # Use @> containment: department_ids @> '["dept"]' to check membership.
                import json
                from sqlalchemy import cast, func, or_
                from sqlalchemy.dialects.postgresql import JSONB
                dept_arr = cast(json.dumps([dept]), JSONB)
                kb_q = kb_q.filter(
                    or_(
                        func.jsonb_array_length(KnowledgeDocument.department_ids) == 0,
                        KnowledgeDocument.department_ids.op("@>")(dept_arr),
                        )
                )
            for r in kb_q.all():
                results.append({
                    "id":         f"live-kb-{r.id}",
                    "type":       "kb_approval",
                    "title":      f"[KB] New doc pending: {r.name}",
                    "body":       "",
                    "source_id":  str(r.id),
                    "metadata":   {"entity_type": "kb_doc", "entity_id": str(r.id),
                                   "entity_name": r.filename, "display_name": r.name,
                                   "namespace": r.namespace,
                                   "status": "PENDING_APPROVAL", "action": "submit",
                                   "uploaded_by": r.uploaded_by,
                                   "uploaded_at": (r.created_at.isoformat() + ("+00:00" if r.created_at.tzinfo is None else "")) if r.created_at else None},
                    "read":       False,
                    "created_at": _utc_posix(r.created_at),
                    "live":       True,
                })
        except Exception:
            logger.warning("pending-approvals: kb_docs query failed")

    finally:
        db.close()

    # ── Codebase index requests ──────────────────────────────
    # Scoped by dept via: index_requests.product_id → dept_product_mappings.department
    # product_id IS NULL = unscoped repo → admin-only
    try:
        pg_conn = psycopg2.connect(postgres_dsn())
        try:
            with pg_conn.cursor() as cur:
                if is_admin:
                    cur.execute("""
                        SELECT id, repo_name, branch, requested_by, created_at
                        FROM index_requests
                        WHERE status = 'pending'
                        ORDER BY created_at DESC
                    """)
                else:
                    cur.execute("""
                        SELECT ir.id, ir.repo_name, ir.branch, ir.requested_by, ir.created_at
                        FROM index_requests ir
                        JOIN dept_product_mappings dpm ON dpm.product_id = ir.product_id
                        WHERE ir.status = 'pending'
                          AND dpm.department = %s
                        ORDER BY ir.created_at DESC
                    """, (dept,))
                for row in cur.fetchall():
                    req_id, repo_name, branch, requested_by, created_at = row
                    results.append({
                        "id":         f"live-codebase-{req_id}",
                        "type":       "codebase_approval",
                        "title":      f"[Codebase] Index request: {repo_name}",
                        "body":       f"**{requested_by}** has requested indexing of `{repo_name}` (branch: `{branch}`). Review and approve or reject.",
                        "source_id":  str(req_id),
                        "metadata":   {
                            "entity_type":  "codebase",
                            "request_id":   str(req_id),
                            "repo_name":    repo_name,
                            "branch":       branch,
                            "submitted_by": requested_by or "",
                            "action":       "submit",
                        },
                        "read":       False,
                        "created_at": _utc_posix(created_at),
                        "live":       True,
                    })
        finally:
            pg_conn.close()
    except Exception:
        logger.warning("pending-approvals: codebase query failed")

    # ── MCP tools in PENDING_L2 state (IS team approvers only) ─
    if is_l2_approver:
        try:
            import json as _json_inbox
            from core.config import RDB_REGISTRY
            from core.kv import get_kv
            _ri = get_kv(RDB_REGISTRY, decode_responses=True)
            _ri.ping()
            for key in _ri.keys("marketplace:tool:*"):
                raw = _ri.get(key)
                if not raw:
                    continue
                try:
                    td = _json_inbox.loads(raw)
                except Exception:
                    continue
                if td.get("status") != "PENDING_L2":
                    continue
                tname = td.get("name") or key.replace("marketplace:tool:", "")
                results.append({
                    "id":         f"live-mcp-l2-{tname}",
                    "type":       "governance_approval",
                    "title":      f"[IS Review] MCP Tool: {tname}",
                    "body":       f"**MCP Tool `{tname}`** has passed L1 review and requires IS/Security team approval.\n\nRegistered by: `{td.get('registered_by', 'unknown')}`\n\n{td.get('description', '')}",
                    "source_id":  tname,
                    "metadata":   {"entity_type": "mcp", "entity_name": tname,
                                   "status": "PENDING_L2", "action": "approve_l2",
                                   "is_critical": True},
                    "read":       False,
                    "created_at": 0,
                    "live":       True,
                })
        except Exception:
            logger.warning("pending-approvals: PENDING_L2 MCP query failed")

    results.sort(key=lambda x: x["created_at"], reverse=True)
    return {"items": results}


@router.get("/inbox/stream")
def inbox_stream(
        user: str = Query(...),
        current_user: dict = Depends(get_current_user),
):
    """
    SSE endpoint — push real-time inbox notifications to the browser.
    Connect once; events arrive whenever publish_inbox_item() fires for this user.

    Event format:  data: <JSON item>\n\n
    Heartbeat:     : ping\n\n  (every 25 s — keeps connection alive through proxies)

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    previously trusted the ``user`` query param verbatim, letting any caller
    subscribe to another user's real-time inbox notification stream.
    Fix: added `current_user: dict = Depends(get_current_user)` and
    reassigned `user` from `current_user["sub"]` below before it's used to
    open the SSE subscription, discarding the caller-supplied query-param
    value.
    """
    user = current_user.get("sub") or current_user.get("user_id") or user
    from store.inbox_store import _sse_subscribe, _sse_unsubscribe

    def _event_gen():
        q = _sse_subscribe(user)
        try:
            while True:
                try:
                    # Block up to 25 s; if nothing arrives, send a heartbeat
                    item = q.get(timeout=25)
                    yield f"data: {json.dumps(item)}\n\n"
                except Exception:
                    # Timeout — send SSE heartbeat (keeps proxies alive)
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            _sse_unsubscribe(user, q)

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
