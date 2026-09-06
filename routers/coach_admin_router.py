# SPDX-License-Identifier: MIT
"""
AiNxt Coach — admin console API router (prefix /coach/admin).

Every route requires role=admin (require_admin_flag) and is gated by ENABLE_COACH.
Every mutation writes a coach_admin_audit row. Admin reads/writes are department-
scoped where relevant. No raw prompts are ever exposed.

Response shapes are aligned with ai-ui/src/components/CoachAdmin.jsx.

Endpoints:
  GET    /coach/admin/attention           — users needing coaching ({items:[…]})
  GET    /coach/admin/departments         — distinct dept names ({departments:[str]})
  GET    /coach/admin/audit               — admin audit trail ({items:[…]})
  GET    /coach/admin/impact              — org-wide coaching impact metrics
  GET    /coach/admin/cost-vs-practice    — per-user cost-vs-score scatter ({points:[…]})
  POST   /coach/admin/reset               — soft (mute) / hard (delete) a user's hits
  DELETE /coach/admin/purge               — purge a user's coach data (GDPR)
  POST   /coach/admin/rules/disable       — disable a rule (org/dept)
  POST   /coach/admin/rules/enable        — re-enable a rule
  GET    /coach/admin/rules/disabled      — list disabled rules ({items:[…]})
  POST   /coach/admin/coach-user          — send a manual coaching note (+ HTML mail)
  GET    /coach/admin/notes/{user_id}     — notes sent to a user
  POST   /coach/admin/preview-message     — preview a digest/nudge (subject/body/html_body)
  GET    /coach/admin/weekly-mail/status  — weekly mail config + opt-out count
  POST   /coach/admin/weekly-mail/opt-out — opt a user out
  DELETE /coach/admin/weekly-mail/opt-out/{user_id} — opt a user back in
  GET    /coach/admin/weekly-mail/opt-outs — list opt-outs ({items:[…], total})
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth.dependencies import get_current_user
from auth.rbac import require_admin_flag
from core.pii_crypto import encrypt_pii
from core.config import (
    ENABLE_COACH, COACH_WEEKLY_MAIL_ENABLED,
    COACH_WEEKLY_MAIL_WEEKDAY, COACH_WEEKLY_MAIL_HOUR_IST, COACH_WEEKLY_MAIL_MIN_IST,
)
from db.database import SessionLocal
from core.security_validation import validate_coach_note_request, _flatten_errors

logger = logging.getLogger("coach.admin")
router = APIRouter(tags=["coach-admin"])


# ── guards / helpers ─────────────────────────────────────────────────────────

def _require_enabled():
    if not ENABLE_COACH:
        raise HTTPException(status_code=404, detail="AiNxt Coach is not enabled")
    return True


def _uid(user: dict) -> str:
    return user.get("sub") or user.get("user_id") or user.get("email") or ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> Optional[str]:
    if not dt:
        return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _user_meta(db, user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Resolve a batch of coach user_ids (email OR uuid/sub) → {email,name,department}.

    Returns a dict keyed by the ORIGINAL user_id so callers can enrich rows
    without changing identity. Best-effort: unknown ids map to {} (frontend
    falls back to showing the raw user_id).
    """
    out: Dict[str, Dict[str, Any]] = {}
    ids = [u for u in {u for u in user_ids if u} if u]
    if not ids:
        return out
    try:
        from db.models import User
        emails = [u for u in ids if "@" in u]
        others = [u for u in ids if "@" not in u]
        rows = []
        if emails:
            rows += db.query(User).filter(User.email.in_(emails)).all()
        if others:
            rows += db.query(User).filter(User.id.in_(others)).all()
        by_email = {r.email: r for r in rows}
        by_id = {r.id: r for r in rows}
        for uid in ids:
            r = by_email.get(uid) or by_id.get(uid)
            if r:
                out[uid] = {"email": encrypt_pii(r.email), "name": encrypt_pii(r.name), "department": r.department}
    except Exception:
        logger.debug("coach.admin: user meta resolve failed")
    return out


def _resolve_coach_user_id(db, user_id: str) -> str:
    """Return the canonical user_id (UUID/sub) used in coach tables.

    Coach events/hits store the JWT `sub` claim, which is the User UUID.
    Admin UIs often send the email. Resolve email → UUID; pass UUID through.
    """
    if not user_id:
        return user_id
    if "@" not in user_id:
        return user_id
    try:
        from db.models import User
        row = db.query(User).filter(User.email == user_id).first()
        if row:
            return str(row.id)
    except Exception:
        logger.warning("coach.admin: user id resolve failed")
    return user_id


def _audit(db, actor: dict, action: str, *, target_user: str = None,
           rule_id: str = None, details: dict = None, reason: str = ""):
    try:
        from db.models import CoachAdminAudit
        db.add(CoachAdminAudit(
            actor_id=_uid(actor),
            actor_email=actor.get("email"),
            action=action,
            target_user=target_user,
            rule_id=rule_id,
            details=details or {},
            reason=(reason or "")[:2000],
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("coach.admin: audit write failed")


# ── request models ───────────────────────────────────────────────────────────

class DisableRuleIn(BaseModel):
    rule_id: str
    department: Optional[str] = None   # None = org-wide
    reason: Optional[str] = ""


class EnableRuleIn(BaseModel):
    rule_id: str
    department: Optional[str] = None


class CoachUserIn(BaseModel):
    user_id: str
    kind: str = "coaching_note"        # unified — kind field kept for DB audit trail
    subject: Optional[str] = None
    body: Optional[str] = None
    custom_note: Optional[str] = None  # optional admin-written note appended to the message
    include_task_analysis: bool = False  # include per-domain task breakdown in the message
    days: int = 30                     # analysis window — must match the UI time-picker


class PreviewMessageIn(BaseModel):
    user_id: str
    kind: str = "coaching_note"
    custom_note: Optional[str] = None
    include_task_analysis: bool = False
    days: int = 30                     # analysis window — must match the UI time-picker


class ResetIn(BaseModel):
    user_id: str
    days: int = 30
    category: Optional[str] = None
    mode: str = "soft"                 # soft = mute hits | hard = delete hits
    reason: Optional[str] = ""


class PurgeIn(BaseModel):
    user_id: str
    days: int = 180
    reason: Optional[str] = ""


class OptOutIn(BaseModel):
    user_id: str
    reason: Optional[str] = ""


# ── GET /attention ───────────────────────────────────────────────────────────

@router.get("/coach/admin/attention")
def admin_attention(days: int = Query(30, ge=1, le=365),
                    limit: int = Query(25, ge=1, le=200),
                    current_user: dict = Depends(require_admin_flag)):
    """Users with the most (un-muted) rule violations this window, enriched with
    email/name, critical-hit count, PII-event count, and their top rule."""
    _require_enabled()
    from sqlalchemy import func
    from db.models import CoachRuleHit

    since = _now() - timedelta(days=days)
    db = SessionLocal()
    try:
        # Top offenders by total hits.
        totals = (db.query(CoachRuleHit.user_id,
                           func.count(CoachRuleHit.id))
                  .filter(CoachRuleHit.created_at >= since,
                          CoachRuleHit.muted == False)  # noqa: E712
                  .group_by(CoachRuleHit.user_id)
                  .order_by(func.count(CoachRuleHit.id).desc())
                  .limit(limit).all())
        user_ids = [t[0] for t in totals]
        if not user_ids:
            return {"days": days, "items": []}

        # Critical-severity counts per user.
        crit = dict(db.query(CoachRuleHit.user_id, func.count(CoachRuleHit.id))
                    .filter(CoachRuleHit.created_at >= since,
                            CoachRuleHit.muted == False,  # noqa: E712
                            CoachRuleHit.severity == "critical",
                            CoachRuleHit.user_id.in_(user_ids))
                    .group_by(CoachRuleHit.user_id).all())
        # PII-category counts per user.
        pii = dict(db.query(CoachRuleHit.user_id, func.count(CoachRuleHit.id))
                   .filter(CoachRuleHit.created_at >= since,
                           CoachRuleHit.muted == False,  # noqa: E712
                           CoachRuleHit.category == "security",
                           CoachRuleHit.rule_id == "security.pii_in_prompt",
                           CoachRuleHit.user_id.in_(user_ids))
                   .group_by(CoachRuleHit.user_id).all())
        # Top rule per user.
        rule_rows = (db.query(CoachRuleHit.user_id, CoachRuleHit.rule_id,
                              func.count(CoachRuleHit.id))
                     .filter(CoachRuleHit.created_at >= since,
                             CoachRuleHit.muted == False,  # noqa: E712
                             CoachRuleHit.user_id.in_(user_ids))
                     .group_by(CoachRuleHit.user_id, CoachRuleHit.rule_id).all())
        from agents.coach_evaluator import RULES_BY_ID
        ruleById = {rid: r.to_meta() for rid, r in RULES_BY_ID.items()}
        top_rule: Dict[str, Dict[str, Any]] = {}
        for uid, rid, n in rule_rows:
            cur = top_rule.get(uid)
            if cur is None or int(n) > cur["n"]:
                top_rule[uid] = {
                    "rule_id": rid,
                    "code": (ruleById.get(rid) or {}).get("code") or rid,
                    "n": int(n),
                }

        meta = _user_meta(db, user_ids)
        items = []
        for uid, hits in totals:
            m = meta.get(uid, {})
            items.append({
                "user_id": uid,
                "email": m.get("email"),
                "name": m.get("name"),
                "department": m.get("department"),
                "hits": int(hits),
                "critical": int(crit.get(uid, 0)),
                "pii_events": int(pii.get(uid, 0)),
                "top_rule": top_rule.get(uid),
            })
        return {"days": days, "items": items}
    finally:
        db.close()


# ── GET /departments ─────────────────────────────────────────────────────────

@router.get("/coach/admin/departments")
def admin_departments(days: int = Query(90, ge=1, le=365),
                      current_user: dict = Depends(require_admin_flag)):
    """Distinct department names that have any coach activity in the window.

    Returns a plain string list — the frontend dropdown filters on it."""
    _require_enabled()
    from db.models import CoachEvent

    since = _now() - timedelta(days=days)
    db = SessionLocal()
    try:
        rows = (db.query(CoachEvent.department)
                .filter(CoachEvent.ts >= since)
                .distinct().all())
        depts = sorted({(r[0] or "").strip() for r in rows if r[0] and r[0].strip()},
                       key=lambda x: x.lower())
        return {"days": days, "departments": depts}
    finally:
        db.close()


# ── GET /audit ───────────────────────────────────────────────────────────────

@router.get("/coach/admin/audit")
def admin_audit(days: int = Query(30, ge=1, le=365),
                limit: int = Query(100, ge=1, le=500),
                current_user: dict = Depends(require_admin_flag)):
    _require_enabled()
    from db.models import CoachAdminAudit
    since = _now() - timedelta(days=days)
    db = SessionLocal()
    try:
        rows = (db.query(CoachAdminAudit)
                .filter(CoachAdminAudit.created_at >= since)
                .order_by(CoachAdminAudit.created_at.desc())
                .limit(limit).all())
        return {"items": [{
            "id": r.id, "actor_id": r.actor_id, "actor_email": r.actor_email,
            "action": r.action, "target_user": r.target_user, "rule_id": r.rule_id,
            "details": r.details or {}, "reason": r.reason,
            "ts": _iso(r.created_at),
        } for r in rows]}
    finally:
        db.close()


# ── GET /impact ──────────────────────────────────────────────────────────────

@router.get("/coach/admin/impact")
def admin_impact(days: int = Query(30, ge=1, le=365),
                 current_user: dict = Depends(require_admin_flag)):
    """Org-wide coaching impact for the window: how many events were observed,
    how many rule hits were raised, how many PII leaks were blocked, how many
    vague prompts were coached, total spend, and a per-category hit breakdown."""
    _require_enabled()
    from sqlalchemy import func
    from db.models import CoachEvent, CoachRuleHit

    since = _now() - timedelta(days=days)
    db = SessionLocal()
    try:
        events = (db.query(func.count(CoachEvent.event_id))
                  .filter(CoachEvent.ts >= since).scalar()) or 0
        spend = (db.query(func.coalesce(func.sum(CoachEvent.cost_usd), 0.0))
                 .filter(CoachEvent.ts >= since).scalar()) or 0.0
        # Filter muted=False so admin counts match what individual users see in
        # their own Overview (which also excludes muted hits). Muted hits are
        # still stored in the DB for audit purposes but excluded from all counts.
        rule_hits = (db.query(func.count(CoachRuleHit.id))
                     .filter(CoachRuleHit.created_at >= since,
                             CoachRuleHit.muted == False)  # noqa: E712
                     .scalar()) or 0
        pii_blocked = (db.query(func.count(CoachRuleHit.id))
                       .filter(CoachRuleHit.created_at >= since,
                               CoachRuleHit.muted == False,  # noqa: E712
                               CoachRuleHit.rule_id == "security.pii_in_prompt").scalar()) or 0
        vague = (db.query(func.count(CoachRuleHit.id))
                 .filter(CoachRuleHit.created_at >= since,
                         CoachRuleHit.muted == False,  # noqa: E712
                         CoachRuleHit.rule_id == "prompt.vague").scalar()) or 0
        by_cat = (db.query(CoachRuleHit.category, func.count(CoachRuleHit.id))
                  .filter(CoachRuleHit.created_at >= since,
                          CoachRuleHit.muted == False)  # noqa: E712
                  .group_by(CoachRuleHit.category)
                  .order_by(func.count(CoachRuleHit.id).desc()).all())
        return {
            "days": days,
            "events": int(events),
            "rule_hits": int(rule_hits),
            "pii_leaks_blocked": int(pii_blocked),
            "vague_prompts_coached": int(vague),
            "total_spend_usd": round(float(spend or 0.0), 2),
            "hits_by_category": [
                {"category": c or "unknown", "count": int(n)} for c, n in by_cat
            ],
        }
    finally:
        db.close()


# ── GET /cost-vs-practice ────────────────────────────────────────────────────

_QUADRANT_SCORE_CUT = 60.0  # right of 60 = good practice

def _quadrant(score: Optional[float], cost: float, cost_median: float) -> str:
    sc = score if isinstance(score, (int, float)) else 0.0
    high_cost = cost >= cost_median and cost > 0
    good = sc >= _QUADRANT_SCORE_CUT
    if high_cost and not good:
        return "high_cost_low_practice"
    if high_cost and good:
        return "high_cost_good_practice"
    if (not high_cost) and (not good):
        return "low_cost_low_practice"
    return "healthy"


@router.get("/coach/admin/cost-vs-practice")
def admin_cost_vs_practice(days: int = Query(30, ge=1, le=365),
                           limit: int = Query(200, ge=1, le=500),
                           current_user: dict = Depends(require_admin_flag)):
    """Per-user (cost, practice-score) points for a scatter plot, each tagged
    with a quadrant and enriched with email/name/department."""
    _require_enabled()
    from sqlalchemy import func
    from db.models import CoachEvent
    from agents.coach_evaluator import compute_scores

    since = _now() - timedelta(days=days)
    db = SessionLocal()
    try:
        rows = (db.query(CoachEvent.user_id,
                         func.coalesce(func.sum(CoachEvent.cost_usd), 0.0),
                         func.count(CoachEvent.event_id))
                .filter(CoachEvent.ts >= since)
                .group_by(CoachEvent.user_id)
                .order_by(func.sum(CoachEvent.cost_usd).desc())
                .limit(limit).all())
        if not rows:
            return {"days": days, "points": []}

        costs = sorted(float(c or 0.0) for _, c, _ in rows)
        cost_median = costs[len(costs) // 2] if costs else 0.0

        meta = _user_meta(db, [r[0] for r in rows])
        points = []
        for uid, cost, n in rows:
            sc = compute_scores(uid, days=days, db=db)
            score = sc.get("overall")
            cost_f = round(float(cost or 0.0), 4)
            m = meta.get(uid, {})
            points.append({
                "user_id": uid,
                "email": m.get("email"),
                "name": m.get("name"),
                "department": m.get("department"),
                "cost_usd": cost_f,
                "event_count": int(n),
                "score": score,
                "quadrant": _quadrant(score, cost_f, cost_median),
            })
        return {"days": days, "points": points}
    finally:
        db.close()


# ── POST /reset (soft = mute hits, hard = delete hits) ────────────────────────

@router.post("/coach/admin/reset")
def admin_reset(body: ResetIn, current_user: dict = Depends(require_admin_flag)):
    """Reset a user's score for the window.

    soft → mute the matching rule hits (kept for audit, excluded from scoring).
    hard → permanently delete the matching rule hits.
    Always purges any score snapshots so the next compute is fresh.
    """
    _require_enabled()
    from db.models import CoachRuleHit, CoachScoreSnapshot

    mode = (body.mode or "soft").lower()
    if mode not in ("soft", "hard"):
        raise HTTPException(status_code=400, detail="mode must be 'soft' or 'hard'")

    since = _now() - timedelta(days=max(1, body.days))
    db = SessionLocal()
    try:
        target_user_id = _resolve_coach_user_id(db, body.user_id)
        q = (db.query(CoachRuleHit)
             .filter(CoachRuleHit.user_id == target_user_id,
                     CoachRuleHit.created_at >= since))
        if body.category:
            q = q.filter(CoachRuleHit.category == body.category)

        if mode == "hard":
            affected = q.delete(synchronize_session=False)
        else:
            affected = q.filter(CoachRuleHit.muted == False).update(  # noqa: E712
                {CoachRuleHit.muted: True}, synchronize_session=False)

        # Fresh snapshots either way.
        (db.query(CoachScoreSnapshot)
         .filter(CoachScoreSnapshot.user_id == body.user_id)
         .delete(synchronize_session=False))
        db.commit()

        _audit(db, current_user, f"reset_score:{type(mode).__name__}", target_user=target_user_id,
               details={"days": body.days, "category": body.category,
                        "affected_hits": int(affected),
                        "input_user_id": body.user_id},
               reason=body.reason or "")
        return {"ok": True, "mode": mode, "user_id": target_user_id,
                "affected_hits": int(affected)}
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"reset failed")
    finally:
        db.close()


# ── DELETE /purge ────────────────────────────────────────────────────────────

@router.delete("/coach/admin/purge")
def admin_purge(body: PurgeIn, current_user: dict = Depends(require_admin_flag)):
    """Purge a user's coach data older than `days` (events, hits, snapshots,
    notes) — GDPR / right-to-erasure."""
    _require_enabled()
    from db.models import (CoachEvent, CoachRuleHit, CoachScoreSnapshot,
                           CoachManualNote)
    cutoff = _now() - timedelta(days=max(0, body.days))
    db = SessionLocal()
    try:
        target_user_id = _resolve_coach_user_id(db, body.user_id)
        hits_deleted = (db.query(CoachRuleHit)
                        .filter(CoachRuleHit.user_id == target_user_id,
                                CoachRuleHit.created_at < cutoff)
                        .delete(synchronize_session=False))
        events_deleted = (db.query(CoachEvent)
                          .filter(CoachEvent.user_id == target_user_id,
                                  CoachEvent.ts < cutoff)
                          .delete(synchronize_session=False))
        (db.query(CoachManualNote)
         .filter(CoachManualNote.user_id == target_user_id,
                 CoachManualNote.created_at < cutoff)
         .delete(synchronize_session=False))
        (db.query(CoachScoreSnapshot)
         .filter(CoachScoreSnapshot.user_id == target_user_id)
         .delete(synchronize_session=False))
        db.commit()
        _audit(db, current_user, "purge_events", target_user=target_user_id,
               details={"events_deleted": int(events_deleted),
                        "hits_deleted": int(hits_deleted),
                        "days": body.days,
                        "input_user_id": body.user_id},
               reason=body.reason or "")
        return {"ok": True,
                "user_id": target_user_id,
                "events_deleted": int(events_deleted),
                "hits_deleted": int(hits_deleted)}
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"purge failed")
    finally:
        db.close()


# ── POST /rules/disable ──────────────────────────────────────────────────────

@router.post("/coach/admin/rules/disable")
def admin_disable_rule(body: DisableRuleIn, current_user: dict = Depends(require_admin_flag)):
    _require_enabled()
    from agents.coach_evaluator import RULES_BY_ID
    from db.models import CoachRuleDisabled
    if body.rule_id not in RULES_BY_ID:
        raise HTTPException(status_code=404, detail=f"unknown rule {body.rule_id}")
    db = SessionLocal()
    try:
        exists = (db.query(CoachRuleDisabled)
                  .filter(CoachRuleDisabled.rule_id == body.rule_id,
                          CoachRuleDisabled.department == body.department)
                  .first())
        if not exists:
            db.add(CoachRuleDisabled(rule_id=body.rule_id, department=body.department,
                                     reason=body.reason or "", disabled_by=_uid(current_user)))
            db.commit()
        _audit(db, current_user, "disable_rule", rule_id=body.rule_id,
               details={"department": body.department}, reason=body.reason or "")
        return {"ok": True, "rule_id": body.rule_id, "department": body.department}
    finally:
        db.close()


# ── POST /rules/enable ───────────────────────────────────────────────────────

@router.post("/coach/admin/rules/enable")
def admin_enable_rule(body: EnableRuleIn, current_user: dict = Depends(require_admin_flag)):
    _require_enabled()
    from db.models import CoachRuleDisabled
    db = SessionLocal()
    try:
        q = db.query(CoachRuleDisabled).filter(CoachRuleDisabled.rule_id == body.rule_id)
        if body.department is not None:
            q = q.filter(CoachRuleDisabled.department == body.department)
        deleted = q.delete(synchronize_session=False)
        db.commit()
        _audit(db, current_user, "enable_rule", rule_id=body.rule_id,
               details={"department": body.department, "rows": int(deleted)})
        return {"ok": True, "rule_id": body.rule_id, "removed": int(deleted)}
    finally:
        db.close()


# ── GET /rules/disabled ──────────────────────────────────────────────────────

@router.get("/coach/admin/rules/disabled")
def admin_disabled_rules(current_user: dict = Depends(require_admin_flag)):
    _require_enabled()
    from db.models import CoachRuleDisabled
    db = SessionLocal()
    try:
        rows = db.query(CoachRuleDisabled).order_by(CoachRuleDisabled.created_at.desc()).all()
        return {"items": [{
            "id": r.id, "rule_id": r.rule_id, "department": r.department,
            "reason": r.reason, "disabled_by": r.disabled_by,
            "ts": _iso(r.created_at),
        } for r in rows]}
    finally:
        db.close()


# ── task-type analysis ────────────────────────────────────────────────────────

# Domain → human-readable label
_DOMAIN_LABELS: Dict[str, str] = {
    "code":    "Coding / Development",
    "devops":  "DevOps / Infrastructure",
    "data":    "Data & Analytics",
    "security":"Security",
    "finance": "Finance",
    "hr":      "HR / People",
    "legal":   "Legal / Compliance",
    "general": "General / Non-coding",
}

# Per-domain improvement tips keyed by coach category
_DOMAIN_TIPS: Dict[str, Dict[str, str]] = {
    "code": {
        "prompt-quality":      "For coding tasks, include the language, framework version, and the exact error message or expected behaviour in your prompt.",
        "session-hygiene":     "Keep coding sessions focused on one feature or bug at a time; start a new thread when switching context.",
        "review-discipline":   "Always review AI-generated code before committing — check edge cases, error handling, and security implications.",
        "tool-mastery":        "Use code-aware tools (file references, diff views) rather than pasting large blocks of code into the chat.",
        "context-management":  "Attach the relevant file or function directly instead of describing it; this reduces token waste and improves accuracy.",
        "security":            "Never paste API keys, credentials, or PII into coding prompts — use placeholder names and sanitise before sharing.",
    },
    "devops": {
        "prompt-quality":      "Specify the target environment (OS, cloud provider, tool version) and the exact command or config that is failing.",
        "session-hygiene":     "Separate infrastructure planning from execution in different threads to keep context clean.",
        "review-discipline":   "Validate AI-generated scripts in a staging environment before applying to production.",
        "tool-mastery":        "Reference config files directly rather than copy-pasting; use structured output formats (YAML/JSON) for reproducibility.",
        "context-management":  "Provide only the relevant config section, not the entire file, to stay within context limits.",
        "security":            "Redact secrets, tokens, and internal hostnames before sharing infrastructure configs.",
    },
    "data": {
        "prompt-quality":      "Describe the schema, sample rows, and the exact transformation or aggregation you need.",
        "session-hygiene":     "Keep data exploration and data modelling in separate threads.",
        "review-discipline":   "Verify AI-generated SQL or pandas code against a small sample before running on the full dataset.",
        "tool-mastery":        "Use structured data references (table names, column types) rather than pasting raw CSV data.",
        "context-management":  "Share only the relevant columns and a few representative rows, not the entire dataset.",
        "security":            "Anonymise or mask PII fields before including any data samples in your prompts.",
    },
    "general": {
        "prompt-quality":      "Be specific about the format and length of the answer you need — vague questions produce vague answers.",
        "session-hygiene":     "Start a new conversation for each distinct topic to avoid context bleed.",
        "review-discipline":   "Fact-check AI responses for non-coding tasks, especially for dates, statistics, and legal/financial claims.",
        "tool-mastery":        "Explore available tools (web search, document upload) to ground the AI's answers in real sources.",
        "context-management":  "Summarise long background information rather than pasting it verbatim to save context space.",
        "security":            "Avoid sharing personal, confidential, or company-sensitive information in general chat sessions.",
    },
}

# Fallback tips for domains not explicitly listed above
_GENERIC_TIPS: Dict[str, str] = {
    "prompt-quality":      "Write clear, specific prompts that include the goal, constraints, and expected output format.",
    "session-hygiene":     "Keep each session focused on a single topic and start fresh threads when switching tasks.",
    "review-discipline":   "Always review AI output critically before acting on it.",
    "tool-mastery":        "Explore the full range of available tools to get more accurate and grounded answers.",
    "context-management":  "Share only the context that is directly relevant to your question.",
    "security":            "Never include credentials, PII, or confidential data in your prompts.",
}


# ── purpose-built task-type classifier ───────────────────────────────────────
# The existing detect_query_domain() was designed for RAG routing, not task
# analysis. Its WRITE_CODE_PATTERN intentionally marks "write a function" as
# "general" (meaning: don't search the repo). That is wrong for our purpose —
# "write a Python function" IS a coding task. We use our own regex set here.

import re as _re

# Signals that strongly indicate a coding / development task.
# Covers: asking AI to write/fix/debug/review code, mentioning languages,
# frameworks, error types, code constructs, and file types.
_CODE_SIGNALS = _re.compile(
    r"""
    # Explicit coding verbs
    \b(write|implement|create|build|generate|fix|debug|refactor|review|
       optimise|optimize|test|deploy|compile|run|execute|parse|serialize|
       deserialize|migrate|scaffold|lint|format)\b.*\b(code|function|method|
       class|script|module|api|endpoint|query|schema|test|component|hook|
       service|controller|model|view|route|middleware|pipeline|workflow)\b
    |
    # Language / framework names
    \b(python|javascript|typescript|java|kotlin|swift|go|golang|rust|c\+\+|
       csharp|c#|ruby|php|scala|r\b|sql|bash|shell|powershell|html|css|
       react|vue|angular|django|flask|fastapi|spring|express|nextjs|nuxt|
       rails|laravel|dotnet|\.net|node\.?js|pytorch|tensorflow|pandas|
       numpy|sklearn|scikit|spark|kafka|redis|postgres|mysql|mongodb|
       graphql|grpc|rest\s*api|openapi|swagger)\b
    |
    # Code constructs / error types / auth implementation concepts
    \b(function|class|method|variable|constant|loop|recursion|async|await|
       promise|callback|lambda|closure|decorator|annotation|interface|
       abstract|inheritance|polymorphism|exception|stacktrace|traceback|
       null\s*pointer|index\s*out\s*of\s*bounds|type\s*error|syntax\s*error|
       import|package|dependency|library|framework|sdk|cli|dockerfile|
       kubernetes|k8s|helm|terraform|ansible|ci/?cd|github\s*actions|
       jenkins|pytest|junit|jest|mocha|cypress|selenium|
       jwt|oauth|oauth2|saml|authentication|authorisation|authorization|
       token|session|cookie|middleware|cors|csrf\s*token)\b
    |
    # File extensions in context
    \.(py|js|ts|jsx|tsx|java|kt|go|rs|rb|php|cs|cpp|c|h|sh|yaml|yml|
       json|xml|sql|html|css|scss|tf|dockerfile)\b
    |
    # Common coding question patterns
    \b(how\s+do\s+i|how\s+to|what\s+is\s+the\s+best\s+way\s+to)\b.{0,60}
    \b(code|function|class|api|database|query|script|algorithm|data\s*structure)\b
    """,
    _re.IGNORECASE | _re.VERBOSE,
)

# Signals for DevOps / infrastructure tasks.
_DEVOPS_SIGNALS = _re.compile(
    r"\b(docker|kubernetes|k8s|helm|terraform|ansible|puppet|chef|"
    r"ci/?cd|pipeline|jenkins|github\s*actions|gitlab\s*ci|circleci|"
    r"nginx|apache|load\s*balancer|reverse\s*proxy|ssl|tls|certificate|"
    r"deployment|container|pod|cluster|node|replica|autoscal|"
    r"aws|azure|gcp|cloud|s3|ec2|lambda|serverless|vpc|subnet|"
    r"monitoring|prometheus|grafana|elk|splunk|datadog|pagerduty|"
    r"bash\s*script|shell\s*script|cron|systemd|service\s*restart)\b",
    _re.IGNORECASE,
)

# Signals for data / analytics tasks.
_DATA_SIGNALS = _re.compile(
    r"\b(sql|query|select|join|aggregate|group\s*by|pivot|etl|"
    r"data\s*pipeline|data\s*warehouse|data\s*lake|spark|kafka|"
    r"pandas|numpy|dataframe|csv|excel|tableau|power\s*bi|looker|"
    r"machine\s*learning|ml\s*model|neural\s*network|training|"
    r"feature\s*engineering|data\s*cleaning|data\s*analysis|"
    r"statistics|regression|classification|clustering|nlp|"
    r"big\s*data|hadoop|hive|airflow|dbt|snowflake|redshift|bigquery)\b",
    _re.IGNORECASE,
)

# Signals for security tasks.
# Note: generic words like "authentication" are intentionally excluded here —
# "implement authentication" is a coding task, not a security-domain task.
# We require more specific security-domain vocabulary.
_SECURITY_SIGNALS = _re.compile(
    r"\b(vulnerability|exploit|penetration\s*test|pentest|cve|"
    r"owasp|xss|sql\s*injection|csrf|"
    r"firewall|ids|ips|siem|threat\s*model|malware|ransomware|phishing|"
    r"security\s*audit|security\s*review|security\s*scan|"
    r"pci\s*dss|soc\s*2|nist|"
    r"password\s*policy|mfa|2fa|brute\s*force|"
    r"secret\s*leak|credential\s*leak|api\s*key\s*leak|"
    r"privilege\s*escalation|zero\s*day|supply\s*chain\s*attack)\b",
    _re.IGNORECASE,
)

# Signals for finance tasks.
_FINANCE_SIGNALS = _re.compile(
    r"\b(invoice|budget|expense|revenue|profit|loss|balance\s*sheet|"
    r"p&l|cash\s*flow|forecast|financial|accounting|payroll|tax|"
    r"reimbursement|purchase\s*order|vendor|procurement|cost\s*centre|"
    r"roi|kpi|quarterly|fiscal|audit|ledger|reconciliation)\b",
    _re.IGNORECASE,
)

# Signals for HR / people tasks.
_HR_SIGNALS = _re.compile(
    r"\b(employee|onboarding|offboarding|leave|holiday|pto|performance\s*review|"
    r"appraisal|recruitment|hiring|interview|job\s*description|salary|"
    r"compensation|benefits|hr\s*policy|code\s*of\s*conduct|disciplinary|"
    r"grievance|team\s*building|org\s*chart|headcount|attrition|retention)\b",
    _re.IGNORECASE,
)

# Signals for legal / compliance tasks.
_LEGAL_SIGNALS = _re.compile(
    r"\b(contract|agreement|nda|terms\s*of\s*service|privacy\s*policy|"
    r"legal|compliance|regulation|gdpr|hipaa|clause|liability|"
    r"intellectual\s*property|patent|trademark|copyright|litigation|"
    r"dispute|arbitration|jurisdiction|indemnity|warranty|sla)\b",
    _re.IGNORECASE,
)


def _classify_prompt(text: str) -> str:
    """Classify a prompt into a task domain for coaching analysis.

    Priority order (most specific first):
      security > devops > data > finance > hr > legal > code > general

    This classifier is purpose-built for task analysis — it correctly
    identifies coding work regardless of whether the user is asking AI to
    *write* code or asking *about* code concepts. It does NOT use the
    detect_query_domain() RAG-routing classifier, which intentionally
    misclassifies "write a function" as 'general'.
    """
    if not text or not text.strip():
        return "general"
    t = text.strip()
    # Security is highest priority — it can overlap with code/devops
    if _SECURITY_SIGNALS.search(t):
        return "security"
    if _DEVOPS_SIGNALS.search(t):
        return "devops"
    if _DATA_SIGNALS.search(t):
        return "data"
    if _FINANCE_SIGNALS.search(t):
        return "finance"
    if _HR_SIGNALS.search(t):
        return "hr"
    if _LEGAL_SIGNALS.search(t):
        return "legal"
    if _CODE_SIGNALS.search(t):
        return "code"
    return "general"


def analyze_task_types(db, user_id: str, days: int = 30) -> Dict[str, Any]:
    """Classify a user's recent prompts by domain and return a breakdown with
    targeted improvement advice based on their rule-hit patterns.

    Returns:
        {
          "total_events": int,
          "domains": [
            {
              "domain": str,          # e.g. "code"
              "label": str,           # e.g. "Coding / Development"
              "count": int,
              "pct": float,           # 0–100
              "top_issues": [         # up to 3 rule categories with hits in this domain
                {"category": str, "count": int, "tip": str}
              ],
            },
            …
          ],
          "summary": str,             # one-paragraph plain-text summary
        }
    """
    from datetime import timedelta
    from db.models import CoachEvent, CoachRuleHit
    from services.coach_ingestor.crypto import decrypt

    since = _now() - timedelta(days=max(1, days))

    # Load events with their encrypted prompts
    events = (db.query(CoachEvent)
              .filter(CoachEvent.user_id == user_id,
                      CoachEvent.ts >= since)
              .order_by(CoachEvent.ts.desc())
              .limit(500)          # cap to avoid very long classification loops
              .all())

    if not events:
        return {
            "total_events": 0,
            "domains": [],
            "summary": "No activity found in this period — no task analysis available.",
        }

    # Classify each event
    domain_counts: Dict[str, int] = {}
    event_domains: Dict[str, str] = {}  # event_id → domain
    for ev in events:
        prompt_text = decrypt(ev.prompt_redacted) or ""
        domain = _classify_prompt(prompt_text[:1000])  # truncate for speed
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        event_domains[str(ev.event_id)] = domain

    total = sum(domain_counts.values())

    # Load rule hits for this user in the same window
    hits = (db.query(CoachRuleHit)
            .filter(CoachRuleHit.user_id == user_id,
                    CoachRuleHit.created_at >= since,
                    CoachRuleHit.muted == False)  # noqa: E712
            .all())

    # Map event_id → list of hit categories
    hit_cats_by_event: Dict[str, List[str]] = {}
    for h in hits:
        eid = str(h.event_id)
        hit_cats_by_event.setdefault(eid, []).append(h.category)

    # Aggregate hit categories per domain
    domain_hit_cats: Dict[str, Dict[str, int]] = {}
    for ev in events:
        eid = str(ev.event_id)
        dom = event_domains.get(eid, "general")
        cats = hit_cats_by_event.get(eid, [])
        for cat in cats:
            domain_hit_cats.setdefault(dom, {})
            domain_hit_cats[dom][cat] = domain_hit_cats[dom].get(cat, 0) + 1

    # Build output
    domains_out = []
    for domain, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
        pct = round(count / total * 100, 1) if total else 0.0
        label = _DOMAIN_LABELS.get(domain, domain.replace("-", " ").title())
        cat_hits = domain_hit_cats.get(domain, {})
        tips_map = _DOMAIN_TIPS.get(domain, _GENERIC_TIPS)
        top_issues = []
        for cat, cnt in sorted(cat_hits.items(), key=lambda x: -x[1])[:3]:
            tip = tips_map.get(cat) or _GENERIC_TIPS.get(cat, "")
            top_issues.append({"category": cat, "count": cnt, "tip": tip})
        domains_out.append({
            "domain": domain,
            "label": label,
            "count": count,
            "pct": pct,
            "top_issues": top_issues,
        })

    # Plain-text summary
    if len(domains_out) == 1:
        d = domains_out[0]
        summary = (
            f"All {total} interaction(s) this period were classified as "
            f"'{d['label']}'. "
        )
    else:
        top = domains_out[0]
        summary = (
            f"Over the last {days} days, {total} interaction(s) were analysed. "
            f"The dominant task type was '{top['label']}' ({top['pct']}%). "
        )
        if len(domains_out) > 1:
            others = ", ".join(
                f"'{d['label']}' ({d['pct']}%)" for d in domains_out[1:3]
            )
            summary += f"Other task types included {others}. "

    has_issues = any(d["top_issues"] for d in domains_out)
    if has_issues:
        summary += "Targeted improvement tips per task type are included below."
    else:
        summary += "No recurring issues were detected for any task type — great work!"

    return {
        "total_events": total,
        "domains": domains_out,
        "summary": summary,
    }


# ── GET /task-analysis ────────────────────────────────────────────────────────

@router.get("/coach/admin/task-analysis")
def admin_task_analysis(
    user_id: str = Query(..., description="User email or UUID"),
    days: int = Query(30, ge=1, le=365),
    current_user: dict = Depends(require_admin_flag),
):
    """Classify a user's recent prompts by task domain and return a breakdown
    with targeted improvement advice per domain.

    No raw prompt text is ever returned — only the domain label and coaching tips.
    """
    _require_enabled()
    db = SessionLocal()
    try:
        target_user_id = _resolve_coach_user_id(db, user_id)
        result = analyze_task_types(db, target_user_id, days=days)
        return {"user_id": target_user_id, "days": days, **result}
    finally:
        db.close()


# ── manual coaching helpers (subject/body/html) ──────────────────────────────

def _coach_message(db, user_id: str, kind: str = "coaching_note",
                   custom_note: Optional[str] = None,
                   include_task_analysis: bool = False,
                   days: int = 30) -> Dict[str, Any]:
    """Build a unified coaching message from a user's real scores + recommendations.

    Produces one consistent format regardless of `kind` (kept only for the DB
    audit trail). The message always contains:
      • Overall practice score
      • Events analysed count
      • Top opportunities (up to 5 rule-hit recommendations)
      • Optional task-type breakdown with per-domain improvement tips
      • Optional admin-written custom note appended at the end
    """
    from agents.coach_evaluator import compute_scores
    from agents.coach_recommender import recommend_for_user

    days = max(1, int(days))
    scores = compute_scores(user_id, days=days, db=db)
    recs = recommend_for_user(user_id, days=days, limit=5, db=db)

    overall = scores.get("overall")
    overall_str = (f"{overall:.0f}/100" if isinstance(overall, (int, float))
                   else "n/a (not enough activity yet)")
    event_count = scores.get("event_count", 0)

    subject = "Your AiNxt Coach summary"

    lines = [
        f"Overall practice score: {overall_str}",
        f"Events analysed: {event_count}",
        "",
        "Top opportunities:",
    ]
    if recs:
        for i, r in enumerate(recs, 1):
            lines.append(f"{i}. {r['title']} — {r['advice']}")
    else:
        lines.append("No recurring issues to flag this period — nicely done.")

    # Optional task-type analysis section
    task_analysis: Optional[Dict[str, Any]] = None
    if include_task_analysis:
        try:
            task_analysis = analyze_task_types(db, user_id, days=days)
            if task_analysis and task_analysis.get("total_events", 0) > 0:
                lines += ["", "─" * 40, "Task-type breakdown:", task_analysis["summary"]]
                for d in task_analysis.get("domains", []):
                    lines.append(f"\n{d['label']} ({d['pct']}% of activity, {d['count']} interaction(s)):")
                    if d.get("top_issues"):
                        for issue in d["top_issues"]:
                            lines.append(f"  • [{issue['category']}] {issue['tip']}")
                    else:
                        lines.append("  • No issues detected for this task type — keep it up!")
        except Exception:
            logger.warning("coach.admin: task analysis failed")

    if custom_note and custom_note.strip():
        lines += ["", "Note from your coach:", custom_note.strip()]

    body_text = "\n".join(lines)

    html_body = None
    usage = None
    try:
        from workers.coach_weekly_mail_worker import _build_html, _coach_usage
        usage = _coach_usage(db, user_id, days=days)
        html_body = _build_html(user_id, scores, recs, usage,
                                custom_note=custom_note or "",
                                task_analysis=task_analysis)
    except Exception:
        logger.debug("coach.admin: html render unavailable")

    return {
        "subject": subject,
        "body": body_text,
        "html_body": html_body,
        "overall_score": overall,
        "usage": usage,
        "task_analysis": task_analysis,
    }


# ── POST /coach-user (manual note + HTML mail) ───────────────────────────────

@router.post("/coach/admin/coach-user")
def admin_coach_user(body: CoachUserIn, current_user: dict = Depends(require_admin_flag)):
    _require_enabled()

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # admin-typed subject/body/custom_note that override the generated
    # coaching message.
    is_valid, field_errors, sanitized = validate_coach_note_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.subject = sanitized["subject"]
    body.body = sanitized["body"]
    body.custom_note = sanitized["custom_note"]

    from db.models import CoachManualNote
    db = SessionLocal()
    try:
        target_user_id = _resolve_coach_user_id(db, body.user_id)
        msg = _coach_message(db, target_user_id,
                             kind=body.kind or "coaching_note",
                             custom_note=body.custom_note,
                             include_task_analysis=body.include_task_analysis,
                             days=body.days)
        subject = body.subject if body.subject is not None else msg["subject"]
        text = body.body if body.body is not None else msg["body"]

        note = CoachManualNote(
            user_id=target_user_id, actor_id=_uid(current_user),
            actor_email=current_user.get("email"),
            kind=body.kind or "nudge", subject=subject or "",
            body=text or "", delivered=False,
        )
        db.add(note)
        db.commit()
        note_id = note.id

        # Deliver via Inbox (fire-and-forget).
        delivered = False
        try:
            from agents.coach_evaluator import publish_coach_inbox
            publish_coach_inbox(
                target_user_id,
                title=subject or "A note from your AiNxt Coach",
                body=text or "",
                source_id=note_id,
                metadata={
                    "kind": body.kind,
                    "from": current_user.get("email"),
                    "html_body": msg.get("html_body"),
                    "usage": msg.get("usage"),
                    "overall_score": msg.get("overall_score"),
                    "task_analysis": msg.get("task_analysis"),
                },
            )
            note.delivered = True
            delivered = True
            db.commit()
        except Exception:
            logger.warning("coach.admin: note delivery failed")

        # Best-effort HTML email to the user's registered address.
        email_sent = False
        try:
            email = None
            from db.models import User
            u = db.query(User).filter(User.id == target_user_id).first()
            email = u.email if u else (body.user_id if "@" in body.user_id else None)
            if email and msg.get("html_body"):
                from services.smtp_service import send_html_email
                email_sent = bool(send_html_email([email],
                                                  subject or "Your AiNxt Coach",
                                                  msg["html_body"]))
        except Exception:
            logger.debug("coach.admin: mail send skipped")

        _audit(db, current_user, "manual_coach", target_user=target_user_id,
               details={"kind": body.kind, "note_id": note_id, "email_sent": email_sent,
                        "input_user_id": body.user_id})
        return {"ok": True, "user_id": target_user_id, "note_id": note_id,
                "delivered": delivered, "email_sent": email_sent}
    finally:
        db.close()


# ── GET /notes/{type(user_id).__name__} ─────────────────────────────────────────────────────

@router.get("/coach/admin/notes/{type(user_id).__name__}")
def admin_notes(user_id: str, current_user: dict = Depends(require_admin_flag)):
    _require_enabled()
    from db.models import CoachManualNote
    db = SessionLocal()
    try:
        target_user_id = _resolve_coach_user_id(db, user_id)
        rows = (db.query(CoachManualNote)
                .filter(CoachManualNote.user_id == target_user_id)
                .order_by(CoachManualNote.created_at.desc()).all())
        return {"user_id": target_user_id, "items": [{
            "id": r.id, "kind": r.kind, "subject": r.subject, "body": r.body,
            "actor_email": r.actor_email, "delivered": r.delivered,
            "ts": _iso(r.created_at),
        } for r in rows]}
    finally:
        db.close()


# ── POST /preview-message ────────────────────────────────────────────────────

@router.post("/coach/admin/preview-message")
def admin_preview_message(body: PreviewMessageIn, current_user: dict = Depends(require_admin_flag)):
    """Render a draft coaching message (subject, plain body, HTML body) from the
    user's current scores + top issues."""
    _require_enabled()

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_coach_note_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.custom_note = sanitized["custom_note"]

    db = SessionLocal()
    try:
        target_user_id = _resolve_coach_user_id(db, body.user_id)
        msg = _coach_message(db, target_user_id,
                             kind=body.kind or "coaching_note",
                             custom_note=body.custom_note,
                             include_task_analysis=body.include_task_analysis,
                             days=body.days)
        return {
            "user_id": target_user_id, "kind": body.kind,
            "subject": msg["subject"],
            "body": msg["body"],
            "html_body": msg["html_body"],
            "overall_score": msg["overall_score"],
            "task_analysis": msg.get("task_analysis"),
        }
    finally:
        db.close()


# ── weekly mail ──────────────────────────────────────────────────────────────

@router.get("/coach/admin/weekly-mail/status")
def admin_weekly_status(current_user: dict = Depends(require_admin_flag)):
    _require_enabled()
    from db.models import CoachWeeklyMailOptOut
    db = SessionLocal()
    try:
        opt_outs = db.query(CoachWeeklyMailOptOut).count()
        return {
            "enabled": COACH_WEEKLY_MAIL_ENABLED,
            "weekday": COACH_WEEKLY_MAIL_WEEKDAY,
            "hour_ist": COACH_WEEKLY_MAIL_HOUR_IST,
            "min_ist": COACH_WEEKLY_MAIL_MIN_IST,
            "opt_out_count": int(opt_outs),
        }
    finally:
        db.close()


@router.post("/coach/admin/weekly-mail/opt-out")
def admin_weekly_opt_out(body: OptOutIn, current_user: dict = Depends(require_admin_flag)):
    _require_enabled()
    from db.models import CoachWeeklyMailOptOut
    db = SessionLocal()
    try:
        target_user_id = _resolve_coach_user_id(db, body.user_id)
        exists = (db.query(CoachWeeklyMailOptOut)
                  .filter(CoachWeeklyMailOptOut.user_id == target_user_id).first())
        already = bool(exists)
        if not exists:
            db.add(CoachWeeklyMailOptOut(user_id=target_user_id,
                                         opted_out_by=_uid(current_user),
                                         reason=body.reason or ""))
            db.commit()
        _audit(db, current_user, "weekly_opt_out", target_user=target_user_id,
               reason=body.reason or "",
               details={"input_user_id": body.user_id})
        return {"ok": True, "user_id": target_user_id, "already_opted_out": already}
    finally:
        db.close()


@router.delete("/coach/admin/weekly-mail/opt-out/{type(user_id).__name__}")
def admin_weekly_opt_in(user_id: str, current_user: dict = Depends(require_admin_flag)):
    _require_enabled()
    from db.models import CoachWeeklyMailOptOut
    db = SessionLocal()
    try:
        target_user_id = _resolve_coach_user_id(db, user_id)
        deleted = (db.query(CoachWeeklyMailOptOut)
                   .filter(CoachWeeklyMailOptOut.user_id == target_user_id)
                   .delete(synchronize_session=False))
        db.commit()
        _audit(db, current_user, "weekly_opt_in", target_user=target_user_id,
               details={"rows": int(deleted), "input_user_id": user_id})
        return {"ok": True, "user_id": target_user_id, "removed": int(deleted)}
    finally:
        db.close()


@router.get("/coach/admin/weekly-mail/opt-outs")
def admin_weekly_opt_outs(limit: int = Query(100, ge=1, le=500),
                          current_user: dict = Depends(require_admin_flag)):
    _require_enabled()
    from db.models import CoachWeeklyMailOptOut
    db = SessionLocal()
    try:
        rows = (db.query(CoachWeeklyMailOptOut)
                .order_by(CoachWeeklyMailOptOut.created_at.desc())
                .limit(limit).all())
        meta = _user_meta(db, [r.user_id for r in rows])
        items = []
        for r in rows:
            m = meta.get(r.user_id, {})
            items.append({
                "id": r.id, "user_id": r.user_id,
                "email": m.get("email"), "name": m.get("name"),
                "opted_out_by": r.opted_out_by, "reason": r.reason,
                "ts": _iso(r.created_at),
            })
        return {"items": items, "total": len(items)}
    finally:
        db.close()
