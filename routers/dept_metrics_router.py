# SPDX-License-Identifier: MIT
# ============================================================
# DEPARTMENT METRICS ROUTER
# Admin-only endpoints for department-level usage analytics.
#
# GET /dept-metrics/departments         — list all departments
# GET /dept-metrics/{dept}?days=7       — aggregated stats for a dept
# GET /dept-metrics/{dept}/models       — per-model breakdown for a dept
# GET /dept-metrics/summary             — cross-department summary
# ============================================================

from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text as _text

router = APIRouter(prefix="/dept-metrics", tags=["dept-metrics"])


def _get_db():
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/departments")
def list_departments(db=Depends(_get_db)):
    """Return all distinct departments from org_tree (or users table fallback)."""
    try:
        rows = db.execute(_text(
            "SELECT DISTINCT department FROM users WHERE department IS NOT NULL AND department != '' ORDER BY department"
        )).fetchall()
        return {"departments": [r[0] for r in rows]}
    except Exception:
        return {"departments": []}


@router.get("/summary")
def dept_summary(days: int = Query(7, ge=1, le=90), db=Depends(_get_db)):
    """Cross-department summary — total tokens, cost, requests, avg latency."""
    try:
        rows = db.execute(_text("""
            SELECT
                u.department,
                COUNT(mu.id)                        AS total_requests,
                COALESCE(SUM(mu.total_tokens), 0)   AS total_tokens,
                COALESCE(SUM(mu.cost_usd), 0)       AS total_cost_usd,
                COALESCE(AVG(mu.latency_ms), 0)     AS avg_latency_ms
            FROM model_usages mu
            JOIN users u ON u.id::text = mu.user_id
            WHERE mu.created_at >= NOW() - INTERVAL '1 day' * :days
            GROUP BY u.department
            ORDER BY total_tokens DESC
        """), {"days": days}).fetchall()
        return {"days": days, "departments": [dict(r._mapping) for r in rows]}
    except Exception as e:
        return {"days": days, "departments": [], "error": str(e)}


@router.get("/{dept}")
def dept_stats(
    dept: str,
    days: int = Query(7, ge=1, le=90),
    db=Depends(_get_db),
):
    """Aggregated usage stats for a single department."""
    try:
        row = db.execute(_text("""
            SELECT
                COUNT(mu.id)                        AS total_requests,
                COALESCE(SUM(mu.total_tokens), 0)   AS total_tokens,
                COALESCE(SUM(mu.input_tokens), 0)   AS input_tokens,
                COALESCE(SUM(mu.output_tokens), 0)  AS output_tokens,
                COALESCE(SUM(mu.cost_usd), 0)       AS total_cost_usd,
                COALESCE(AVG(mu.latency_ms), 0)     AS avg_latency_ms,
                COUNT(DISTINCT mu.user_id)          AS unique_users
            FROM model_usages mu
            JOIN users u ON u.id::text = mu.user_id
            WHERE u.department = :dept
              AND mu.created_at >= NOW() - INTERVAL '1 day' * :days
        """), {"dept": dept, "days": days}).fetchone()

        # Daily breakdown
        daily = db.execute(_text("""
            SELECT
                DATE(mu.created_at)                 AS day,
                COUNT(mu.id)                        AS requests,
                COALESCE(SUM(mu.total_tokens), 0)   AS tokens,
                COALESCE(SUM(mu.cost_usd), 0)       AS cost_usd
            FROM model_usages mu
            JOIN users u ON u.id::text = mu.user_id
            WHERE u.department = :dept
              AND mu.created_at >= NOW() - INTERVAL '1 day' * :days
            GROUP BY DATE(mu.created_at)
            ORDER BY day DESC
        """), {"dept": dept, "days": days}).fetchall()

        return {
            "department": dept,
            "days":        days,
            "summary":     dict(row._mapping) if row else {},
            "daily":       [dict(r._mapping) for r in daily],
        }
    except Exception as e:
        return {"department": dept, "days": days, "summary": {}, "daily": [], "error": str(e)}


@router.get("/{dept}/models")
def dept_model_breakdown(
    dept: str,
    days: int = Query(7, ge=1, le=90),
    db=Depends(_get_db),
):
    """Per-model usage breakdown for a department."""
    try:
        rows = db.execute(_text("""
            SELECT
                mu.model,
                COUNT(mu.id)                        AS requests,
                COALESCE(SUM(mu.total_tokens), 0)   AS tokens,
                COALESCE(SUM(mu.cost_usd), 0)       AS cost_usd,
                COALESCE(AVG(mu.latency_ms), 0)     AS avg_latency_ms
            FROM model_usages mu
            JOIN users u ON u.id::text = mu.user_id
            WHERE u.department = :dept
              AND mu.created_at >= NOW() - INTERVAL '1 day' * :days
            GROUP BY mu.model
            ORDER BY tokens DESC
        """), {"dept": dept, "days": days}).fetchall()
        return {"department": dept, "days": days, "models": [dict(r._mapping) for r in rows]}
    except Exception as e:
        return {"department": dept, "days": days, "models": [], "error": str(e)}


@router.get("/{dept}/evals")
def dept_eval_summary(
    dept: str,
    days: int = Query(7, ge=1, le=90),
    db=Depends(_get_db),
):
    """Eval quality scores for a department."""
    try:
        rows = db.execute(_text("""
            SELECT
                DATE(created_at)            AS day,
                ROUND(AVG(grounding)::numeric, 3)     AS avg_grounding,
                ROUND(AVG(completeness)::numeric, 3)  AS avg_completeness,
                ROUND(AVG(chunk_count)::numeric, 1)   AS avg_chunks,
                COUNT(*)                    AS total_evals
            FROM eval_scores
            WHERE department = :dept
              AND created_at >= NOW() - INTERVAL '1 day' * :days
            GROUP BY DATE(created_at)
            ORDER BY day DESC
        """), {"dept": dept, "days": days}).fetchall()
        return {"department": dept, "days": days, "evals": [dict(r._mapping) for r in rows]}
    except Exception as e:
        return {"department": dept, "days": days, "evals": [], "error": str(e)}
