# SPDX-License-Identifier: Apache-2.0
"""
AiNxt Coach — user-facing API router (prefix /coach).

Self-contained: every route is gated by ENABLE_COACH. The router is read-mostly
— it surfaces a user's own practice scores, usage, rule hits, recommendations,
and an org rollup. The only writes are the dry-run rule tester (no persistence)
and the on-demand LLM prompt-rewrite suggestion.

Scoping: a user always sees their OWN data (user_id from JWT). Department rollup
is scoped to the caller's department. No raw prompts are ever returned — only the
redacted/encrypted form is stored, and endpoints return decrypted-redacted text.

Response shapes mirror the Coach.jsx front-end contract:
  • /dashboard         → overall + per-category scores, totals, by_channel, top_rules
  • /usage             → by_model / by_channel arrays (pct/tokens/cost) + totals (donuts)
  • /events            → flat list OR session-grouped (group_by=thread) with per-prompt
                         rule hits + a per-prompt model recommendation
  • /rules             → catalog with id/name/severity/category/remediation/example_prompt
  • POST /suggest      → LLM (or rule-based) prompt rewrite for a single event
  • POST /rules/test   → dry-run evaluation (no persistence)
  • /org/rollup        → department-scoped events/hits breakdown (events_by_department,
                         hits_by_category, hits_by_severity)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.config import ENABLE_COACH
from db.database import SessionLocal

logger = logging.getLogger("coach.router")
router = APIRouter(tags=["coach"])


# ── guards / helpers ─────────────────────────────────────────────────────────

def _require_enabled():
    if not ENABLE_COACH:
        raise HTTPException(status_code=404, detail="AiNxt Coach is not enabled")
    return True


def _uid(user: dict) -> str:
    return user.get("sub") or user.get("user_id") or user.get("email") or ""


def _dept(user: dict) -> Optional[str]:
    return user.get("department")


def _iso(ts) -> Optional[str]:
    return ts.isoformat() if ts else None


# ── model-recommendation helper (per-prompt, for Query Explorer) ─────────────


def _is_local_model(name: Optional[str]) -> bool:
    if not name:
        return False
    n = name.lower()
    return (n == "local" or n.startswith("local (") or "local-llm" in n
            or "ollama" in n or "in-house" in n or "kimi" in n
            or "glm-" in n or "qwen" in n or "llama" in n)


def _is_block_marker(name: Optional[str]) -> bool:
    n = (name or "").lower().strip()
    return n in {"budget_blocked", "budget_exceeded", "compliance_blocked"}


def _out_cost(model: Optional[str]) -> Optional[float]:
    if not model:
        return None
    if _is_local_model(model):
        return 0.0
    try:
        from core.model_registry import MODEL_COST_PER_1M
        c = MODEL_COST_PER_1M.get(_model_key(model))
        return float(c[1]) if c else None
    except Exception:
        return None


def _model_key(name: Optional[str]) -> str:
    if not name:
        return ""
    text = name.strip()
    matches = re.findall(r"\(([^()]+)\)", text)
    if matches:
        text = matches[-1].strip()
    return text.lower()


def _recommendation_for(prompt_redacted: str, used_model: Optional[str]) -> Optional[Dict[str, Any]]:
    """Use the platform auto-router decision as the per-prompt recommendation."""
    text = (prompt_redacted or "").strip()
    if not text:
        return None
    try:
        from models.model_router import model_router
        decision = model_router.route(text, model_hint=None)
    except Exception:
        return None

    rec_model = decision.model
    rec_tier = decision.tier
    used_cost = _out_cost(used_model)
    rec_cost = _out_cost(_model_key(rec_model))
    verdict = "unknown"
    hint = ""
    if used_model and not _is_block_marker(used_model):
        if _is_local_model(used_model) and rec_tier == "simple":
            verdict, hint = "good_local", "A local/free model handled a simple prompt — ideal."
        elif _model_key(used_model) == _model_key(rec_model):
            verdict = "match"
        elif used_cost is not None and rec_cost is not None:
            if used_cost > rec_cost + 0.01 and rec_tier in ("simple", "medium"):
                verdict = "over_spent"
                hint = (f"This looks {type(rec_tier).__name__}; a cheaper model would do. "
                        f"You used a model that costs ${used_cost:.2f}/1M out vs ${rec_cost:.2f}.")
            elif used_cost + 0.01 < rec_cost and rec_tier == "complex":
                verdict = "under_spent"
                hint = "This looks complex — a stronger model may give better results."
            else:
                verdict = "different_tier"
        else:
            verdict = "different_tier"

    return {
        "recommended_model": rec_model,
        "tier": rec_tier,
        "verdict": verdict,
        "confidence": None,
        "reason": f"Auto-router selected '{type(rec_tier).__name__}' for this prompt (complexity: {decision.complexity}).",
        "hint": hint,
    }


# ── models ───────────────────────────────────────────────────────────────────

class RuleTestIn(BaseModel):
    # The Playground card posts {event: {...}}; the inline tester posts flat fields.
    event: Optional[Dict[str, Any]] = None
    prompt: Optional[str] = ""
    model: Optional[str] = None
    channel: Optional[str] = "web"
    context_window_pct: Optional[float] = 0.0
    tool_calls: Optional[list] = None
    pii_flags: Optional[List[str]] = None
    secret_flags: Optional[List[str]] = None
    compliance_flags: Optional[List[str]] = None
    governance_flags: Optional[List[str]] = None
    rules: Optional[List[str]] = None          # restrict to subset
    ctx: Optional[Dict[str, Any]] = None       # cross-event aggregates


class SuggestIn(BaseModel):
    event_id: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None


# ── GET /my-digest ───────────────────────────────────────────────────────────
# Single endpoint that aggregates everything the "My Digest" tab needs:
#   • overall + per-category scores
#   • top recommendations (from rule-hit history)
#   • task-type breakdown (classify prompts by domain)
#   • usage by model and channel
#   • top rule violations with remediation advice
# One round-trip, no admin privileges required.

@router.get("/coach/my-digest")
def coach_my_digest(days: int = Query(30, ge=1, le=365),
                    current_user: dict = Depends(get_current_user)):
    _require_enabled()
    from sqlalchemy import func
    from agents.coach_evaluator import compute_scores, RULES_BY_ID
    from agents.coach_recommender import recommend_for_user
    from db.models import CoachEvent, CoachRuleHit

    uid = _uid(current_user)
    days = max(1, int(days))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    db = SessionLocal()
    try:
        # ── Scores ──────────────────────────────────────────────────────────
        scores = compute_scores(uid, days=days, db=db)
        overall = scores.get("overall")
        categories = scores.get("categories") or {}
        event_count = int(scores.get("event_count") or 0)

        # ── Recommendations ─────────────────────────────────────────────────
        recs_raw = recommend_for_user(uid, days=days, limit=5, db=db)
        recs = [{
            "rule_id":  r.get("rule_id", ""),
            "category": r.get("category", ""),
            "severity": r.get("severity", "low"),
            "title":    r.get("title", ""),
            "advice":   r.get("advice", ""),
            "count":    int(r.get("count", 0)),
            "impact":   r.get("impact", 0),
        } for r in recs_raw]

        # ── Usage by model + channel ─────────────────────────────────────────
        totals_row = (db.query(
                          func.count(CoachEvent.event_id),
                          func.coalesce(func.sum(CoachEvent.tokens_in), 0),
                          func.coalesce(func.sum(CoachEvent.tokens_out), 0),
                          func.coalesce(func.sum(CoachEvent.cost_usd), 0.0),
                      )
                      .filter(CoachEvent.user_id == uid, CoachEvent.ts >= since)
                      .one())
        total_events_db = int(totals_row[0] or 0)

        model_rows = (db.query(
                          CoachEvent.model,
                          func.count(CoachEvent.event_id),
                          func.coalesce(func.sum(CoachEvent.cost_usd), 0.0),
                      )
                      .filter(CoachEvent.user_id == uid, CoachEvent.ts >= since)
                      .group_by(CoachEvent.model)
                      .order_by(func.count(CoachEvent.event_id).desc())
                      .limit(10).all())
        chan_rows = (db.query(
                        CoachEvent.channel,
                        func.count(CoachEvent.event_id),
                        func.coalesce(func.sum(CoachEvent.cost_usd), 0.0),
                    )
                    .filter(CoachEvent.user_id == uid, CoachEvent.ts >= since)
                    .group_by(CoachEvent.channel)
                    .order_by(func.count(CoachEvent.event_id).desc()).all())

        def _pct(n):
            return round((n / total_events_db) * 100, 1) if total_events_db else 0.0

        by_model = [{
            "name":     m or "unknown",
            "count":    int(n),
            "pct":      _pct(int(n)),
            "cost_usd": round(float(c or 0.0), 4),
        } for m, n, c in model_rows]
        by_channel = [{
            "name":     ch or "unknown",
            "count":    int(n),
            "pct":      _pct(int(n)),
            "cost_usd": round(float(c or 0.0), 4),
        } for ch, n, c in chan_rows]

        # ── Top rule violations ──────────────────────────────────────────────
        hit_rows = (db.query(CoachRuleHit.rule_id, CoachRuleHit.category,
                             CoachRuleHit.severity, func.count(CoachRuleHit.id))
                    .filter(CoachRuleHit.user_id == uid,
                            CoachRuleHit.created_at >= since,
                            CoachRuleHit.muted == False)  # noqa: E712
                    .group_by(CoachRuleHit.rule_id, CoachRuleHit.category,
                              CoachRuleHit.severity)
                    .order_by(func.count(CoachRuleHit.id).desc())
                    .limit(10).all())
        ruleById = {rid: r.to_meta() for rid, r in RULES_BY_ID.items()}
        top_violations = [{
            "rule_id":     rid,
            "category":    cat,
            "severity":    sev,
            "count":       int(n),
            "title":       (ruleById.get(rid) or {}).get("title") or rid,
            "advice":      (ruleById.get(rid) or {}).get("advice") or "",
        } for rid, cat, sev, n in hit_rows]

        # ── Task-type analysis ───────────────────────────────────────────────
        task_analysis = None
        try:
            from routers.coach_admin_router import analyze_task_types
            task_analysis = analyze_task_types(db, uid, days=days)
        except Exception:
            logger.debug("coach.my_digest: task analysis skipped")

        return {
            "user_id":          uid,
            "window_days":      days,
            "insufficient_data": bool(scores.get("gated")),
            "overall":          overall,
            "categories":       categories,
            "event_count":      event_count,
            "recs":             recs,
            "top_violations":   top_violations,
            "usage": {
                "total_events": total_events_db,
                "cost_usd":     round(float(totals_row[3] or 0.0), 6),
                "tokens_in":    int(totals_row[1] or 0),
                "tokens_out":   int(totals_row[2] or 0),
                "by_model":     by_model,
                "by_channel":   by_channel,
            },
            "task_analysis":    task_analysis,
        }
    finally:
        db.close()


# ── GET /dashboard ───────────────────────────────────────────────────────────

@router.get("/coach/dashboard")
def coach_dashboard(days: int = Query(30, ge=1, le=365),
                    current_user: dict = Depends(get_current_user)):
    _require_enabled()
    from sqlalchemy import func
    from agents.coach_evaluator import compute_scores, RULES_BY_ID
    from db.models import CoachEvent, CoachRuleHit

    uid = _uid(current_user)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    db = SessionLocal()
    try:
        scores = compute_scores(uid, days=days, db=db)

        # totals
        event_count = (db.query(func.count(CoachEvent.event_id))
                       .filter(CoachEvent.user_id == uid, CoachEvent.ts >= since)
                       .scalar()) or 0
        hit_count = (db.query(func.count(CoachRuleHit.id))
                     .filter(CoachRuleHit.user_id == uid,
                             CoachRuleHit.created_at >= since,
                             CoachRuleHit.muted == False)  # noqa: E712
                     .scalar()) or 0

        # by channel
        by_channel = (db.query(CoachEvent.channel, func.count(CoachEvent.event_id))
                      .filter(CoachEvent.user_id == uid, CoachEvent.ts >= since)
                      .group_by(CoachEvent.channel)
                      .order_by(func.count(CoachEvent.event_id).desc()).all())

        # top rules
        top_rules = (db.query(CoachRuleHit.rule_id, func.count(CoachRuleHit.id))
                     .filter(CoachRuleHit.user_id == uid,
                             CoachRuleHit.created_at >= since,
                             CoachRuleHit.muted == False)  # noqa: E712
                     .group_by(CoachRuleHit.rule_id)
                     .order_by(func.count(CoachRuleHit.id).desc())
                     .limit(8).all())

        ruleById = {rid: r.to_meta() for rid, r in RULES_BY_ID.items()}
        return {
            "user_id": uid,
            "department": _dept(current_user),
            "window_days": days,
            "insufficient_data": bool(scores.get("gated")),
            "overall": scores.get("overall"),
            "scores": scores.get("categories") or {},
            "totals": {"events": int(event_count), "hits": int(hit_count)},
            "by_channel": [{"channel": c or "unknown", "count": int(n)} for c, n in by_channel],
            "top_rules": [{
                "rule_id":  r,
                "code":     (ruleById.get(r) or {}).get("code") or r,
                "count":    int(n),
                # Include the rule's human-readable metadata so the dashboard
                # "Top Anti-Patterns" card can show what each rule means
                # (title + advice) instead of just the bare code. Falls back
                # to empty strings when the rule isn't in the registry.
                "title":    (ruleById.get(r) or {}).get("title") or "",
                "advice":   (ruleById.get(r) or {}).get("advice") or "",
                "category": (ruleById.get(r) or {}).get("category") or "",
                "severity": (ruleById.get(r) or {}).get("severity") or "low",
            } for r, n in top_rules],
        }
    finally:
        db.close()


# ── GET /usage (donut data: by_model / by_channel arrays + totals) ───────────

@router.get("/coach/usage")
def coach_usage(days: int = Query(30, ge=1, le=365),
                current_user: dict = Depends(get_current_user)):
    _require_enabled()
    from sqlalchemy import func
    from db.models import CoachEvent

    uid = _uid(current_user)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    db = SessionLocal()
    try:
        totals = (db.query(
                      func.count(CoachEvent.event_id),
                      func.coalesce(func.sum(CoachEvent.tokens_in), 0),
                      func.coalesce(func.sum(CoachEvent.tokens_out), 0),
                      func.coalesce(func.sum(CoachEvent.cost_usd), 0.0),
                  )
                  .filter(CoachEvent.user_id == uid, CoachEvent.ts >= since)
                  .one())
        total_events = int(totals[0] or 0)

        model_rows = (db.query(
                          CoachEvent.model,
                          func.count(CoachEvent.event_id),
                          func.coalesce(func.sum(CoachEvent.tokens_in), 0),
                          func.coalesce(func.sum(CoachEvent.tokens_out), 0),
                          func.coalesce(func.sum(CoachEvent.cost_usd), 0.0),
                      )
                      .filter(CoachEvent.user_id == uid, CoachEvent.ts >= since)
                      .group_by(CoachEvent.model)
                      .order_by(func.count(CoachEvent.event_id).desc()).all())
        chan_rows = (db.query(
                         CoachEvent.channel,
                         func.count(CoachEvent.event_id),
                         func.coalesce(func.sum(CoachEvent.tokens_in), 0),
                         func.coalesce(func.sum(CoachEvent.tokens_out), 0),
                         func.coalesce(func.sum(CoachEvent.cost_usd), 0.0),
                     )
                     .filter(CoachEvent.user_id == uid, CoachEvent.ts >= since)
                     .group_by(CoachEvent.channel)
                     .order_by(func.count(CoachEvent.event_id).desc()).all())

        def _pct(n: int) -> float:
            return round((n / total_events) * 100, 1) if total_events else 0.0

        by_model = [{
            "name": m or "unknown",
            "count": int(n),
            "pct": _pct(int(n)),
            "tokens_in": int(ti or 0),
            "tokens_out": int(to or 0),
            "cost_usd": round(float(cost or 0.0), 4),
        } for m, n, ti, to, cost in model_rows]
        by_channel = [{
            "name": c or "unknown",
            "count": int(n),
            "pct": _pct(int(n)),
            "tokens_in": int(ti or 0),
            "tokens_out": int(to or 0),
            "cost_usd": round(float(cost or 0.0), 4),
        } for c, n, ti, to, cost in chan_rows]

        return {
            "user_id": uid,
            "window_days": days,
            "totals": {
                "events": total_events,
                "tokens": int((totals[1] or 0) + (totals[2] or 0)),
                "tokens_in": int(totals[1] or 0),
                "tokens_out": int(totals[2] or 0),
                "cost_usd": round(float(totals[3] or 0.0), 6),
            },
            "by_model": by_model,
            "by_channel": by_channel,
        }
    finally:
        db.close()


# ── GET /rules (catalog with remediation + example) ──────────────────────────

# Per-rule UX metadata the catalog doesn't carry: a short example of a prompt
# that would fire the rule. Remediation maps to the rule's canonical `advice`.
_RULE_EXAMPLE = {
    "prompt.vague":               "fix it",
    "prompt.missing_acceptance":  "write a function that downloads the file",
    "prompt.ambiguous_pronoun":   "fix it and then make that work",
    "prompt.multi_intent":        "add tests and also update docs and also bump version",
    "prompt.missing_constraints": "build a rate limiter that handles lots of traffic",
    "prompt.no_success_def":      "implement the new caching layer across namespaces",
    "session.thread_too_long":    "(a thread with 40+ messages)",
    "session.excess_continue":    "continue",
    "session.stale_resume":       "(resuming a thread after 8h)",
    "review.low_acceptance":      "(most suggestions rejected)",
    "review.unreviewed_apply":    "(applied a diff in < 1.5s)",
    "tool.premium_for_trivial":   "hi there  (on a premium model)",
    "tool.retry_storm":           "(6+ failing tool retries)",
    "tool.unused_tools":          "(manual work a tool could do)",
    "context.saturated":          "(context window > 90%)",
    "context.cross_channel":      "(same task across web/cli/slack)",
    "context.kb_miss":            "(KB returned no match)",
    "context.duplicate_prompt":   "(exact prompt asked before)",
    "security.pii_in_prompt":     "my email is jane@example.com, …",
    "security.secret_in_prompt":  "here is the api key sk-…",
    "security.compliance_block":  "(content tripped a PCI block)",
    "security.governance_flag":   "(governance policy flagged)",
    "security.sensitive_keyword": "what is the admin password again",
}


@router.get("/coach/rules")
def coach_rules(current_user: dict = Depends(get_current_user)):
    _require_enabled()
    from agents.coach_evaluator import rule_catalog, _muted_rule_ids, _disabled_rule_ids

    uid = _uid(current_user)
    db = SessionLocal()
    try:
        muted = _muted_rule_ids(uid, db)
        disabled = _disabled_rule_ids(_dept(current_user), db)
        out = []
        for meta in rule_catalog():
            rid = meta["rule_id"]
            out.append({
                "id": rid,
                "rule_id": rid,
                "code": meta.get("code") or rid,
                "name": meta["title"],
                "category": meta["category"],
                "severity": meta["severity"],
                "remediation": meta["advice"],
                "example_prompt": _RULE_EXAMPLE.get(rid, ""),
                "muted": rid in muted,
                "disabled": rid in disabled,
            })
        return {"rules": out}
    finally:
        db.close()


# ── GET /events (flat OR session-grouped via group_by=thread) ────────────────

def _hit_dicts(event_row, ruleById: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build per-event rule-hit pills from the back-filled rule_hits summary.

    Tolerant of two on-disk shapes for `coach_event.rule_hits`:
      • list[str]  — our evaluator's canonical back-fill (just the rule ids), and
      • list[dict] — legacy/seed rows like {"id": "...", "severity": "..."}.
    Anything unexpected is skipped rather than crashing the whole endpoint.
    """
    out = []
    for entry in (event_row.rule_hits or []):
        if isinstance(entry, dict):
            rid = entry.get("id") or entry.get("rule_id")
            override = entry
        elif isinstance(entry, str):
            rid = entry
            override = {}
        else:
            continue
        if not rid or rid == "__pending__":
            continue
        meta = ruleById.get(rid) or {}
        out.append({
            "id": rid,
            "code": meta.get("code") or rid,
            "name": override.get("name") or meta.get("title") or rid,
            "severity": override.get("severity") or meta.get("severity") or "low",
            "category": override.get("category") or meta.get("category") or "general",
            "advice": override.get("advice") or meta.get("advice") or "",
        })
    return out


@router.get("/coach/events/by-request/{type(request_id).__name__}/hits")
def coach_event_hits_by_request(request_id: str,
                                current_user: dict = Depends(get_current_user)):
    _require_enabled()
    from db.models import CoachEvent
    from agents.coach_evaluator import RULES_BY_ID

    uid = _uid(current_user)
    ruleById = {rid: r.to_meta() for rid, r in RULES_BY_ID.items()}
    db = SessionLocal()
    try:
        row = (db.query(CoachEvent)
               .filter(CoachEvent.user_id == uid,
                       CoachEvent.request_id == request_id)
               .order_by(CoachEvent.ts.desc())
               .first())
        if not row:
            return {"request_id": request_id, "event_id": None, "found": False, "evaluated": False, "rule_hits": []}
        return {
            "request_id": request_id,
            "event_id": row.event_id,
            "found": True,
            "evaluated": row.rule_hits != ["__pending__"],
            "rule_hits": _hit_dicts(row, ruleById),
        }
    finally:
        db.close()


@router.get("/coach/events")
def coach_events(days: int = Query(7, ge=1, le=90),
                 limit: int = Query(50, ge=1, le=200),
                 channel: Optional[str] = None,
                 group_by: Optional[str] = None,
                 recommend: bool = False,
                 current_user: dict = Depends(get_current_user)):
    _require_enabled()
    from db.models import CoachEvent
    from services.coach_ingestor import crypto
    from agents.coach_evaluator import RULES_BY_ID

    uid = _uid(current_user)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    ruleById = {rid: r.to_meta() for rid, r in RULES_BY_ID.items()}

    db = SessionLocal()
    try:
        q = (db.query(CoachEvent)
             .filter(CoachEvent.user_id == uid, CoachEvent.ts >= since))
        if channel:
            q = q.filter(CoachEvent.channel == channel)
        rows = q.order_by(CoachEvent.ts.desc()).limit(limit).all()

        def _serialize(r) -> Dict[str, Any]:
            prompt = crypto.decrypt(r.prompt_redacted)
            # Truncate very long prompts (page snapshots, context dumps) so the
            # Query Explorer doesn't freeze on huge payloads. 500 chars is
            # enough to show the user's task without bloating the response.
            if prompt and len(prompt) > 500:
                prompt = prompt[:497] + "…"
            d = {
                "event_id": r.event_id,
                "ts": _iso(r.ts),
                "channel": r.channel,
                "model": r.model,
                "prompt_redacted": prompt,
                "tokens_in": int(r.tokens_in or 0),
                "tokens_out": int(r.tokens_out or 0),
                "cost_usd": float(r.cost_usd or 0.0),
                "context_window_pct": float(r.context_window_pct or 0.0),
                "accepted": r.accepted,
                "rule_hits": _hit_dicts(r, ruleById),
                "pii_flags": r.pii_flags or [],
                "secret_flags": r.secret_flags or [],
                "compliance_flags": r.compliance_flags or [],
                "thread_id": r.thread_id,
                "project": r.project,
                "_prompt_hash": r.prompt_hash,
                # ── EvalEngine (LLM-as-judge) results — NULL until the async
                #    judge thread completes (~15 s after ingestion).
                "eval_score":   r.eval_score,
                "eval_verdict": r.eval_verdict,
                "eval_issues":  r.eval_issues or [],
            }
            if recommend:
                d["recommendation"] = _recommendation_for(prompt, r.model)
            return d

        # Serialize defensively — one malformed row must never blank the whole
        # Query Explorer (the UI swallows non-200s into an empty session list).
        events = []
        for r in rows:
            try:
                events.append(_serialize(r))
            except Exception:
                logger.warning(
                    "coach.events: skipping event %s",
                    getattr(r, "event_id", "?"),
                )

        if group_by != "thread":
            for ev in events:
                ev.pop("_prompt_hash", None)
            return {"events": events, "count": len(events)}

        # ── group into sessions by thread_id ─────────────────────────────────
        # Primary key: thread_id (set by the web chat session or IDE session_id).
        # Secondary merge: events with NO thread_id (budget-blocked IDE calls,
        # API calls without session tracking) that share the same prompt_hash
        # within a 5-minute window are collapsed into one session — this prevents
        # the same prompt sent from IDE + Web appearing as two separate sessions
        # when the budget gate fires before a thread_id can be established.
        from datetime import datetime as _dt

        def _parse_ts(ts_str: Optional[str]) -> Optional[float]:
            if not ts_str:
                return None
            try:
                return _dt.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                return None

        # Pass 1: bucket by thread_id (events that have one are already grouped).
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        order: List[str] = []
        unthreaded: List[Dict[str, Any]] = []

        for ev in events:
            tid = ev.get("thread_id")
            if tid:
                if tid not in buckets:
                    buckets[tid] = []
                    order.append(tid)
                buckets[tid].append(ev)
            else:
                unthreaded.append(ev)

        # Pass 2: merge unthreaded events by (prompt_hash, 5-min window).
        # Sort oldest-first so the first occurrence anchors the window.
        unthreaded_sorted = sorted(unthreaded, key=lambda e: e.get("ts") or "")
        # Map: synthetic_key → list of events
        unthreaded_buckets: Dict[str, List[Dict[str, Any]]] = {}
        unthreaded_order: List[str] = []
        # Track: prompt_hash → (synthetic_key, anchor_ts) for window matching
        _hash_windows: Dict[str, tuple] = {}  # hash → (key, anchor_ts_float)

        for ev in unthreaded_sorted:
            ph = ev.get("_prompt_hash") or ""
            ev_ch = ev.get("channel") or ""
            ev_ts = _parse_ts(ev.get("ts"))
            merged = False
            if ph and ev_ts is not None and ph in _hash_windows:
                existing_key, anchor_ts, anchor_ch = _hash_windows[ph]
                # Only merge when same channel AND within 5-minute window.
                # Different channels (e.g. mcp vs embed) are separate sessions
                # even when the prompt text is identical.
                if ev_ch == anchor_ch and abs(ev_ts - anchor_ts) <= 300:
                    unthreaded_buckets[existing_key].append(ev)
                    merged = True
            if not merged:
                # New bucket — use event_id as synthetic key
                syn_key = f"__unthreaded__{ev.get('event_id', id(ev))}"
                unthreaded_buckets[syn_key] = [ev]
                unthreaded_order.append(syn_key)
                if ph and ev_ts is not None:
                    _hash_windows[ph] = (syn_key, ev_ts, ev_ch)

        # Merge unthreaded buckets into main order (after threaded sessions)
        for syn_key in unthreaded_order:
            buckets[syn_key] = unthreaded_buckets[syn_key]
            order.append(syn_key)

        sessions = []
        for key in order:
            evs = buckets[key]
            # Sort oldest-first inside the session (DB returned newest-first).
            evs_sorted = sorted(evs, key=lambda e: e["ts"] or "")
            # Deduplicate events within the session by prompt_hash — keeps the
            # first (oldest) occurrence so retries / double-emits don't show as
            # separate rows in Query Explorer.
            seen_hashes: set = set()
            deduped: list = []
            for e in evs_sorted:
                ph = e.get("_prompt_hash") or ""
                if ph and ph in seen_hashes:
                    continue
                if ph:
                    seen_hashes.add(ph)
                deduped.append(e)
            evs_sorted = deduped

            channels = sorted({e["channel"] for e in evs_sorted if e["channel"]})
            client_sources = sorted({_client_source(e["channel"]) for e in evs_sorted
                                     if _client_source(e["channel"])})
            rule_union, pii_u, sec_u, comp_u = {}, set(), set(), set()
            for e in evs_sorted:
                e.pop("_prompt_hash", None)
                for h in e["rule_hits"]:
                    rule_union[h["id"]] = h
                pii_u.update(e.get("pii_flags") or [])
                sec_u.update(e.get("secret_flags") or [])
                comp_u.update(e.get("compliance_flags") or [])
            # Determine the canonical thread_id to expose: use the real thread_id
            # if all events share one, otherwise None (cross-channel merge).
            real_tids = {e.get("thread_id") for e in evs_sorted if e.get("thread_id")}
            canonical_tid = next(iter(real_tids)) if len(real_tids) == 1 else None
            title = _session_title(evs_sorted, key)
            sessions.append({
                "thread_id": canonical_tid,
                "title": title,
                "channels": channels,
                "client_sources": client_sources,
                "event_count": len(evs_sorted),
                "first_ts": evs_sorted[0]["ts"] if evs_sorted else None,
                "last_ts": evs_sorted[-1]["ts"] if evs_sorted else None,
                "tokens_in_total": sum(e["tokens_in"] for e in evs_sorted),
                "tokens_out_total": sum(e["tokens_out"] for e in evs_sorted),
                "cost_usd_total": round(sum(e["cost_usd"] for e in evs_sorted), 6),
                "rule_hits_union": list(rule_union.values()),
                "pii_flags_union": sorted(pii_u),
                "secret_flags_union": sorted(sec_u),
                "compliance_flags_union": sorted(comp_u),
                "events": evs_sorted,
            })
        # Sort sessions newest-last-activity first so the most recently active
        # session always appears at the top of Query Explorer, regardless of
        # which thread_id was first encountered in the DB fetch order.
        sessions.sort(key=lambda s: s["last_ts"] or "", reverse=True)
        return {"sessions": sessions, "count": len(events)}
    finally:
        db.close()


# ── GET /events/{type(event_id).__name__}/recommendation (on-demand, single event) ───────────
#
# The per-prompt model recommendation calls model_router.route(), which may hit
# the LLM-backed complexity classifier (Claude Haiku) when regex confidence is
# low. Computing it for every event during the Query Explorer list load fired up
# to 200 sequential LLM calls per page load — the cause of the Explorer being
# very slow. We now compute it lazily: the list endpoint no longer sets
# recommend=true, and the UI fetches a single recommendation only when the user
# expands one event.
@router.get("/coach/events/{type(event_id).__name__}/recommendation")
def coach_event_recommendation(event_id: str,
                               current_user: dict = Depends(get_current_user)):
    _require_enabled()
    from db.models import CoachEvent
    from services.coach_ingestor import crypto

    uid = _uid(current_user)
    db = SessionLocal()
    try:
        row = (db.query(CoachEvent)
               .filter(CoachEvent.event_id == event_id,
                       CoachEvent.user_id == uid)
               .first())
        if row is None:
            raise HTTPException(status_code=404, detail="event not found")
        prompt = crypto.decrypt(row.prompt_redacted) or ""
        rec = _recommendation_for(prompt, row.model)
        return {"recommendation": rec}
    finally:
        db.close()

# Coarse channel → client-source mapping (no client_source column on CoachEvent).
#
# NOTE: "mcp" is the canonical channel for ALL IDE traffic (VS Code extension,
# JetBrains plugin, Kilo Code, Cline, Cursor, etc.) as well as direct MCP tool
# calls.  We do NOT know which specific IDE client originated the event because
# that information is not stored on CoachEvent.  Returning "ide-vscode" here
# was wrong — it fabricated a "VS Code" label for every MCP event regardless of
# the actual client.  Return the generic "ide" token instead; the frontend maps
# it to the neutral "IDE/API" label via CLIENT_SOURCE_LABEL.
def _client_source(channel: Optional[str]) -> Optional[str]:
    c = (channel or "").lower()
    if c == "mcp":
        return "ide"
    if c == "cli":
        return "cli"
    if c == "api":
        return "api"
    if c == "web":
        return "platform"
    if c == "embed":
        return "browser-ext"
    return None


def _session_title(events: List[Dict[str, Any]], key: str) -> str:
    if key == "__unthreaded__":
        return "Unthreaded prompts"
    for e in events:
        p = (e.get("prompt_redacted") or "").strip()
        if p:
            return p[:80] + ("…" if len(p) > 80 else "")
    return "Session"


# ── POST /rules/test (dry-run, no persist) ───────────────────────────────────

@router.post("/coach/rules/test")
def coach_rules_test(body: RuleTestIn, current_user: dict = Depends(get_current_user)):
    _require_enabled()
    from agents.coach_evaluator import evaluate_dry_run, BASELINE_RULES

    # The Playground posts {event:{...}}; the inline tester posts flat fields.
    src = body.event or {}
    prompt_redacted = src.get("prompt") if body.event else (body.prompt or "")
    prompt_redacted = prompt_redacted or ""
    try:
        from agents.compliance_engine import compliance_engine
        prompt_redacted, _ = compliance_engine.redact_text(prompt_redacted)
    except Exception:
        pass

    event = {
        "user_id": _uid(current_user),
        "channel": src.get("channel") or body.channel or "web",
        "department": _dept(current_user),
        "model": src.get("model") or body.model,
        "prompt_redacted": prompt_redacted,
        "context_window_pct": src.get("context_window_pct", body.context_window_pct) or 0.0,
        "tool_calls": src.get("tool_calls", body.tool_calls) or [],
        "pii_flags": src.get("pii_flags", body.pii_flags) or [],
        "secret_flags": src.get("secret_flags", body.secret_flags) or [],
        "compliance_flags": src.get("compliance_flags", body.compliance_flags) or [],
        "governance_flags": src.get("governance_flags", body.governance_flags) or [],
    }
    hits = evaluate_dry_run(event, ctx=body.ctx or {}, rules=body.rules)
    # Normalise to the {id,name,severity} shape the Playground renders.
    norm = [{"id": h["rule_id"], "name": h["title"], "severity": h["severity"],
             "category": h["category"]} for h in hits]
    return {"hits": norm, "count": len(norm), "evaluated": [r.rule_id for r in BASELINE_RULES]}


# ── POST /suggest (LLM / rule-based prompt rewrite for one event) ─────────────

@router.post("/coach/suggest")
def coach_suggest(body: SuggestIn, current_user: dict = Depends(get_current_user)):
    """Generate a better version of a single prompt. Resolves the prompt from a
    stored event_id (preferred) or an inline prompt, then asks an LLM to rewrite
    it; falls back to a deterministic rule-based rewrite when no LLM is reachable.

    Returns: {rewritten, why, source: llm|fallback|unavailable, notice}
    """
    _require_enabled()
    uid = _uid(current_user)

    prompt = (body.prompt or "").strip()
    model_used = body.model
    if body.event_id:
        from db.models import CoachEvent
        from services.coach_ingestor import crypto
        db = SessionLocal()
        try:
            row = (db.query(CoachEvent)
                   .filter(CoachEvent.event_id == body.event_id,
                           CoachEvent.user_id == uid)
                   .first())
            if row is None:
                raise HTTPException(status_code=404, detail="event not found")
            prompt = crypto.decrypt(row.prompt_redacted) or ""
            model_used = row.model
        finally:
            db.close()

    if not prompt.strip():
        return {"rewritten": "", "why": "", "source": "fallback",
                "notice": "No prompt text to improve."}

    # what the coach would flag — used to ground both LLM + fallback rewrites
    from agents.coach_evaluator import evaluate_dry_run
    hits = evaluate_dry_run({
        "user_id": uid, "channel": "web", "model": model_used,
        "prompt_redacted": prompt,
    })
    issues = [h["title"] for h in hits] or ["clarity and specificity"]

    # 1) Try the LLM rewrite path (best-effort, non-fatal).
    try:
        rewritten, why = _llm_rewrite(prompt, issues)
        if rewritten:
            return {"rewritten": rewritten, "why": why, "source": "llm", "notice": ""}
    except Exception:  # noqa: BLE001
        # SECURITY: exception variable intentionally not referenced in log (CWE-209).
        logger.info("coach.suggest: LLM rewrite unavailable")

    # 2) Deterministic fallback rewrite.
    rewritten, why = _fallback_rewrite(prompt, issues)
    return {"rewritten": rewritten, "why": why, "source": "fallback",
            "notice": "Generated locally (LLM unavailable)."}


def _llm_rewrite(prompt: str, issues: List[str]) -> tuple[str, str]:
    """Ask the platform LLM gateway to rewrite a prompt. Raises on unavailability.

    Uses the existing OpenAIGateway.generate() streaming API (never calls the
    provider SDK directly) and a fast/cheap model. Returns (rewritten, why).
    """
    from gateway_openai import OpenAIGateway
    from core.model_registry import OPENAI_SIMPLE_MODEL

    system = (
        "You are AiNxt Coach. Rewrite the user's AI prompt to be clearer, more "
        "specific, and complete. Keep their intent. Add a concrete goal, the "
        "target file/function if implied, constraints, and a definition of done "
        "when missing. Respond ONLY as compact JSON on a single line: "
        '{"rewritten": "...", "why": "one short sentence"}.'
    )
    user = f"Issues to fix: {', '.join(issues)}.\n\nPrompt:\n{type(prompt).__name__}"

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    gw = OpenAIGateway()
    chunks: List[str] = []
    for tok in gw.generate(messages, model=OPENAI_SIMPLE_MODEL):
        chunks.append(tok)
    raw = "".join(chunks).strip()
    if not raw:
        raise RuntimeError("empty LLM response")

    import json, re
    # Strip code fences if the model wrapped its JSON.
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
        return (data.get("rewritten") or "").strip(), (data.get("why") or "").strip()
    except Exception:
        # Model returned prose, not JSON — use it directly.
        return raw, "Rewritten for clarity and completeness."


def _fallback_rewrite(prompt: str, issues: List[str]) -> tuple[str, str]:
    """Deterministic, no-LLM rewrite: wrap the prompt with a structured scaffold."""
    p = prompt.strip().rstrip(".")
    scaffold = (
        f"{type(p).__name__}.\n\n"
        "Please include:\n"
        "- Goal: <what success looks like>\n"
        "- Target: <file / function / module>\n"
        "- Constraints: <language/version, libraries, performance>\n"
        "- Done when: <acceptance criteria / expected output>"
    )
    return scaffold, "Added explicit goal, target, constraints and acceptance criteria."


# ── GET /org/rollup (department-scoped aggregate) ────────────────────────────

@router.get("/coach/org/rollup")
def coach_org_rollup(days: int = Query(30, ge=1, le=365),
                     department: Optional[str] = None,
                     current_user: dict = Depends(get_current_user)):
    """Department-scoped breakdown. Admins may pass ?department=<name>; everyone
    else is scoped to their own department. Returns events_by_department,
    hits_by_category and hits_by_severity arrays for the bar charts."""
    _require_enabled()
    from sqlalchemy import func
    from db.models import CoachRuleHit, CoachEvent

    is_admin = (current_user.get("role") or "").lower() == "admin"
    if is_admin:
        # Admins: filter to the requested department when given, otherwise show all.
        target_dept = department if department else None
    else:
        # Non-admins: always scoped to their own department.
        target_dept = _dept(current_user)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    db = SessionLocal()
    try:
        ev_q = db.query(CoachEvent.department, func.count(CoachEvent.event_id)) \
                 .filter(CoachEvent.ts >= since)
        hit_q = db.query(CoachRuleHit).filter(CoachRuleHit.created_at >= since,
                                              CoachRuleHit.muted == False)  # noqa: E712
        # Non-admins are always restricted to their own department.
        if target_dept:
            ev_q = ev_q.filter(CoachEvent.department == target_dept)
            hit_q = hit_q.filter(CoachRuleHit.department == target_dept)
        elif not is_admin:
            return {"department": None, "events_by_department": [],
                    "hits_by_category": [], "hits_by_severity": [],
                    "top_rules": [], "event_count": 0}

        events_by_dept = (ev_q.group_by(CoachEvent.department)
                          .order_by(func.count(CoachEvent.event_id).desc()).all())

        hits = hit_q.all()
        cat_counts: Dict[str, int] = {}
        sev_counts: Dict[str, int] = {}
        rule_counts: Dict[str, int] = {}
        for h in hits:
            cat_counts[h.category] = cat_counts.get(h.category, 0) + 1
            sev_counts[h.severity] = sev_counts.get(h.severity, 0) + 1
            rule_counts[h.rule_id] = rule_counts.get(h.rule_id, 0) + 1

        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        return {
            "department": target_dept,
            "days": days,
            "event_count": sum(int(n) for _, n in events_by_dept),
            "events_by_department": [
                {"department": d or "unknown", "count": int(n)} for d, n in events_by_dept
            ],
            "hits_by_category": [
                {"category": c, "count": n}
                for c, n in sorted(cat_counts.items(), key=lambda kv: kv[1], reverse=True)
            ],
            "hits_by_severity": [
                {"severity": s, "count": n}
                for s, n in sorted(sev_counts.items(), key=lambda kv: sev_order.get(kv[0], 9))
            ],
            "top_rules": [
                {"rule_id": r, "count": n}
                for r, n in sorted(rule_counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
            ],
        }
    finally:
        db.close()
