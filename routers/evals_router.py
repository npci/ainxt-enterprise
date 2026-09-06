# SPDX-License-Identifier: MIT
"""
Eval observability endpoints.

GET  /evals/results          — paginated eval results, filterable by type/run/session
GET  /evals/summary          — aggregate stats per eval_type (avg score, pass/warn/fail counts)
GET  /evals/runs/{run_id}    — all evals for a specific SDLC run

Access control: all endpoints require role = "admin".
This mirrors the sidebar's admin-only "Observe" section so the API cannot
be called by non-admins even if they know the URL.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import func
from typing import Optional

router = APIRouter(prefix="/evals", tags=["evals"])


def _get_db():
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _eval_auth():
    """FastAPI dependency: require role = admin (mirrors the admin-only
    Observe section in the sidebar)."""
    from auth.dependencies import get_current_user as _gcu
    async def _check(current_user: dict = Depends(_gcu)):
        role = current_user.get("role", "")
        if role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Eval Observatory requires admin access.",
            )
        return current_user
    return _check


_eval_access = _eval_auth()


@router.get("/results")
def list_eval_results(
    eval_type:  Optional[str] = Query(None, description="Filter by eval type"),
    platform:   Optional[str] = Query(None, description="Filter by source platform (e.g. 'chat', 'knowledge_base')"),
    model:      Optional[str] = Query(None, description="Filter by source model name (groundedness rows only)"),
    session_id: Optional[str] = Query(None),
    run_id:     Optional[str] = Query(None),
    min_score:  Optional[float] = Query(None, ge=0.0, le=1.0),
    max_score:  Optional[float] = Query(None, ge=0.0, le=1.0),
    limit:  int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(_get_db),
    _user: dict = Depends(_eval_access),
):
    from db.models import EvalResult
    q = db.query(EvalResult)
    if eval_type:
        q = q.filter(EvalResult.eval_type == eval_type)
    if platform:
        q = q.filter(EvalResult.platform == platform)
    if model:
        q = q.filter(EvalResult.model == model)
    if session_id:
        q = q.filter(EvalResult.session_id == session_id)
    if run_id:
        q = q.filter(EvalResult.run_id == run_id)
    if min_score is not None:
        q = q.filter(EvalResult.score >= min_score)
    if max_score is not None:
        q = q.filter(EvalResult.score <= max_score)

    total = q.count()
    rows  = q.order_by(EvalResult.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "results": [
            {
                "id":         r.id,
                "eval_type":  r.eval_type,
                "platform":   r.platform,
                "model":      r.model,        # AI source model (the model that generated the answer)
                "judge_model": r.judge_model,  # AI judge model (the model that evaluated the answer)
                "score":      round(r.score, 4),
                "flag":       "PASS" if r.score >= 0.70 else ("WARN" if r.score >= 0.40 else "FAIL"),
                "reason":     r.reason,
                "session_id": r.session_id,
                "run_id":     r.run_id,
                "question":   r.question,
                "metadata":   r.metadata_,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/summary")
def eval_summary(
    hours: int = Query(24, ge=1, le=720, description="Lookback window in hours"),
    platform: Optional[str] = Query(None, description="Filter by source platform (e.g. 'chat', 'knowledge_base')"),
    db=Depends(_get_db),
    _user: dict = Depends(_eval_access),
):
    """Aggregate pass/warn/fail counts and average score per eval_type.

    When platform is omitted, returns aggregates across all platforms (backward compatible).
    When platform is specified, returns aggregates for that platform only.
    """
    from db.models import EvalResult
    import datetime

    since = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)

    # Build base filter — platform filter is additive (backward compatible)
    def _base_filters(since_dt):
        filters = [EvalResult.created_at >= since_dt]
        if platform:
            filters.append(EvalResult.platform == platform)
        return filters

    # Manual aggregation (avoids CAST complexity across DB dialects)
    summary = []
    type_q = db.query(EvalResult.eval_type).filter(*_base_filters(since))
    eval_types = type_q.distinct().all()

    # If the requested window has no data, fall back to all available records
    if not eval_types:
        since = datetime.datetime(2000, 1, 1)  # effectively all-time
        eval_types = db.query(EvalResult.eval_type).filter(
            *([EvalResult.platform == platform] if platform else [])
        ).distinct().all()

    for (et,) in eval_types:
        base = db.query(EvalResult).filter(
            EvalResult.eval_type == et,
            *_base_filters(since),
        )
        total      = base.count()
        avg_score  = db.query(func.avg(EvalResult.score)).filter(
            EvalResult.eval_type == et, *_base_filters(since)
        ).scalar() or 0.0
        pass_count = base.filter(EvalResult.score >= 0.70).count()
        warn_count = base.filter(
            EvalResult.score >= 0.40, EvalResult.score < 0.70
        ).count()
        fail_count = base.filter(EvalResult.score < 0.40).count()

        summary.append({
            "eval_type":   et,
            "total":       total,
            "avg_score":   round(float(avg_score), 4),
            "pass_count":  pass_count,
            "warn_count":  warn_count,
            "fail_count":  fail_count,
            "pass_rate":   round(pass_count / total, 4) if total else 0.0,
        })

    summary.sort(key=lambda x: x["avg_score"])
    return {"hours": hours, "platform": platform, "eval_types": summary}


@router.get("/trend")
def eval_trend(
    days: int = Query(7, ge=1, le=30, description="Number of days to look back"),
    platform: Optional[str] = Query(None, description="Filter by source platform (e.g. 'chat', 'knowledge_base')"),
    db=Depends(_get_db),
    _user: dict = Depends(_eval_access),
):
    """Daily average scores per eval_type for the last N days — used by trend sparklines.

    When platform is omitted, returns trend across all platforms (backward compatible).
    """
    from db.models import EvalResult
    import datetime
    from collections import defaultdict

    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    q = db.query(EvalResult).filter(EvalResult.created_at >= since)
    if platform:
        q = q.filter(EvalResult.platform == platform)
    rows = q.all()

    # Build day labels (oldest → newest)
    today  = datetime.datetime.utcnow()
    labels = [
        (today - datetime.timedelta(days=i)).strftime("%m/%d")
        for i in range(days - 1, -1, -1)
    ]

    # Group scores by (date_label, eval_type)
    buckets: dict = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.created_at:
            day = r.created_at.strftime("%m/%d")
            buckets[day][r.eval_type].append(r.score)

    # Build series per eval_type — None means no data that day
    all_types = sorted({r.eval_type for r in rows})
    series: dict = {}
    for et in all_types:
        series[et] = []
        for label in labels:
            scores = buckets[label].get(et, [])
            series[et].append(round(sum(scores) / len(scores), 3) if scores else None)

    return {"days": days, "labels": labels, "series": series}


@router.get("/model-breakdown")
def eval_model_breakdown(
    hours:    int           = Query(24, ge=1, le=720, description="Lookback window in hours"),
    platform: Optional[str] = Query(None, description="Filter by source platform"),
    db=Depends(_get_db),
    _user: dict = Depends(_eval_access),
):
    """Groundedness (hallucination) score breakdown by source model.

    Returns average groundedness score, pass rate, and total count per model
    so the dashboard can answer "which model hallucinates more?".
    Only groundedness rows with a non-NULL model are included.
    """
    from db.models import EvalResult
    import datetime

    since = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    q = (
        db.query(EvalResult)
        .filter(
            EvalResult.eval_type == "groundedness",
            EvalResult.model.isnot(None),
            EvalResult.created_at >= since,
        )
    )
    if platform:
        q = q.filter(EvalResult.platform == platform)

    rows = q.all()

    # If no data in the window, fall back to all-time
    if not rows:
        q2 = db.query(EvalResult).filter(
            EvalResult.eval_type == "groundedness",
            EvalResult.model.isnot(None),
        )
        if platform:
            q2 = q2.filter(EvalResult.platform == platform)
        rows = q2.all()

    # Aggregate per model
    from collections import defaultdict
    buckets: dict = defaultdict(lambda: {"scores": [], "pass": 0, "total": 0})
    for r in rows:
        b = buckets[r.model]
        b["scores"].append(r.score)
        b["total"] += 1
        if r.score >= 0.70:
            b["pass"] += 1

    breakdown = []
    for model_name, b in buckets.items():
        total = b["total"]
        avg   = sum(b["scores"]) / total if total else 0.0
        breakdown.append({
            "model":      model_name,
            "total":      total,
            "avg_score":  round(avg, 4),
            "pass_count": b["pass"],
            "fail_count": total - b["pass"],
            "pass_rate":  round(b["pass"] / total, 4) if total else 0.0,
        })

    # Sort by avg_score ascending (worst hallucinators first)
    breakdown.sort(key=lambda x: x["avg_score"])
    return {"hours": hours, "platform": platform, "breakdown": breakdown}


@router.get("/chat-quality")
def chat_quality_summary(
    days: int = Query(7, ge=1, le=90),
    user_id: Optional[str] = Query(None),
    db=Depends(_get_db),
    _user: dict = Depends(_eval_access),
):
    """
    Per-response grounding + completeness scores from the eval_scores table.
    Populated automatically for every /ask response.
    """
    from sqlalchemy import text as _text
    try:
        filters = ["created_at >= NOW() - INTERVAL '1 day' * :days"]
        params: dict = {"days": days}
        if user_id:
            filters.append("user_id = :uid")
            params["uid"] = user_id

        where = " AND ".join(filters)
        rows = db.execute(_text(f"""
            SELECT
                DATE(created_at)            AS day,
                ROUND(AVG(grounding)::numeric, 3)     AS avg_grounding,
                ROUND(AVG(completeness)::numeric, 3)  AS avg_completeness,
                ROUND(AVG(chunk_count)::numeric, 1)   AS avg_chunks,
                COUNT(*)                              AS total_responses,
                SUM(CASE WHEN has_context THEN 1 ELSE 0 END) AS responses_with_context
            FROM eval_scores
            WHERE {where}
            GROUP BY DATE(created_at)
            ORDER BY day DESC
        """), params).fetchall()
        return {"days": days, "rows": [dict(r._mapping) for r in rows]}
    except Exception as e:
        return {"days": days, "rows": [], "error": str(e)}


@router.get("/runs/{run_id}")
def evals_for_run(run_id: str, db=Depends(_get_db), _user: dict = Depends(_eval_access)):
    """All eval results for a specific SDLC run, ordered by time."""
    from db.models import EvalResult
    rows = (
        db.query(EvalResult)
        .filter(EvalResult.run_id == run_id)
        .order_by(EvalResult.created_at.asc())
        .all()
    )
    return {
        "run_id": run_id,
        "count":  len(rows),
        "evals":  [
            {
                "eval_type": r.eval_type,
                "score":     round(r.score, 4),
                "flag":      "PASS" if r.score >= 0.70 else ("WARN" if r.score >= 0.40 else "FAIL"),
                "reason":    r.reason,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
