# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt COACH — recommender
# ============================================================
#
# Turns a user's recent coach_rule_hit history into a ranked, de-duplicated
# list of actionable recommendations for the dashboard and the weekly digest.
#
# Ranking signal per rule:
#     impact = Σ severity_weight  (frequency × severity)
# Recommendations are ordered by impact desc, capped, and annotated with the
# canonical advice string from the rule catalog.
#
# Pure read-side logic — no persistence, no LLM calls. Deterministic.
# ============================================================

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.logger import logger

from agents.coach_evaluator import (
    RULES_BY_ID,
    _SEVERITY_WEIGHT,
)


def _severity_rank(sev: str) -> int:
    order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    return order.get(sev, 0)


def recommend_from_hits(hits: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    """Rank + dedup a list of hit dicts into recommendations.

    Each hit dict must have at least: rule_id, category, severity. Optional:
    evidence. Returns recommendations sorted by impact desc.
    """
    agg: Dict[str, Dict[str, Any]] = {}
    impact: Dict[str, float] = defaultdict(float)

    for h in hits:
        rid = h.get("rule_id")
        if not rid:
            continue
        sev = h.get("severity") or "low"
        impact[rid] += _SEVERITY_WEIGHT.get(sev, 1.0)
        if rid not in agg:
            rule = RULES_BY_ID.get(rid)
            agg[rid] = {
                "rule_id": rid,
                "category": h.get("category") or (rule.category if rule else "general"),
                "severity": sev,
                "title": rule.title if rule else h.get("title", rid),
                "advice": rule.advice if rule else h.get("advice", ""),
                "count": 0,
                "samples": [],
            }
        agg[rid]["count"] += 1
        # keep the highest severity seen for this rule
        if _severity_rank(sev) > _severity_rank(agg[rid]["severity"]):
            agg[rid]["severity"] = sev
        ev = h.get("evidence")
        if ev and len(agg[rid]["samples"]) < 3:
            agg[rid]["samples"].append(ev)

    recs = []
    for rid, rec in agg.items():
        rec["impact"] = round(impact[rid], 2)
        recs.append(rec)

    recs.sort(key=lambda r: (r["impact"], _severity_rank(r["severity"]), r["count"]), reverse=True)
    return recs[:limit]


def recommend_for_user(user_id: str, days: int = 30, limit: int = 8, db=None) -> List[Dict[str, Any]]:
    """Load a user's recent hits and produce recommendations. Never raises."""
    own_db = False
    if db is None:
        from db.database import SessionLocal
        db = SessionLocal()
        own_db = True
    try:
        from db.models import CoachRuleHit
        since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        rows = (db.query(CoachRuleHit)
                .filter(CoachRuleHit.user_id == user_id,
                        CoachRuleHit.created_at >= since,
                        CoachRuleHit.muted == False)  # noqa: E712
                .all())
        hits = [{
            "rule_id": r.rule_id,
            "category": r.category,
            "severity": r.severity,
            "evidence": r.evidence,
        } for r in rows]
        return recommend_from_hits(hits, limit=limit)
    except Exception as e:
        logger.error(f"coach.recommender: recommend_for_user failed ({e.__class__.__name__}: {e})")
        return []
    finally:
        if own_db:
            db.close()
