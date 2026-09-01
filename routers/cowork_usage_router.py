# SPDX-License-Identifier: Apache-2.0
"""
Cowork usage analytics + group spend limits (enterprise).

  POST /buddy/usage            — record one turn's cost/tokens (called by the client
                                  after each agent result). Scoped to the caller.
  GET  /buddy/usage            — my month-to-date usage.
  GET  /buddy/usage/analytics  — admin: per-department + per-user month-to-date.
  GET  /buddy/usage/spend      — am I (or my dept) over the group spend limit?
  GET/PUT /buddy/spend-limits   — admin: per-department monthly USD cap.

Spend limits are enforced at the gateway (server office mode) and pre-spawn on
desktop via GET /buddy/usage/spend.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import get_current_user
from auth.rbac import require_admin
from core.logger import logger

router = APIRouter(prefix="/buddy", tags=["buddy"])


def _db():
    from db.database import engine
    from sqlalchemy import text
    return engine, text


# ── Record ────────────────────────────────────────────────────────────────────
class UsageIn(BaseModel):
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: Optional[str] = None
    surface: str = "cowork"   # cowork | office | scheduled


@router.post("/usage", status_code=201)
async def record_usage(body: UsageIn, current_user: dict = Depends(get_current_user)):
    engine, text = _db()
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO cowork_usage (user_id, department, role, surface, model, cost_usd, input_tokens, output_tokens)
                VALUES (:uid, :dept, :role, :surface, :model, :cost, :itok, :otok)
            """), {
                "uid": current_user["sub"], "dept": current_user.get("department") or "",
                "role": current_user.get("role") or "user", "surface": body.surface[:20],
                "model": body.model, "cost": max(0.0, body.cost_usd),
                "itok": max(0, body.input_tokens), "otok": max(0, body.output_tokens),
            })
            # Maintain the DAILY rollup (analytics + spend checks read THIS, not the
            # raw table) — one upserted row per user×dept×surface×day. (scaling)
            conn.execute(text("""
                INSERT INTO cowork_usage_daily (day, department, user_id, surface, cost_usd, tokens, turns)
                VALUES (CURRENT_DATE, :dept, :uid, :surface, :cost, :tok, 1)
                ON CONFLICT (day, department, user_id, surface) DO UPDATE SET
                    cost_usd = cowork_usage_daily.cost_usd + EXCLUDED.cost_usd,
                    tokens   = cowork_usage_daily.tokens   + EXCLUDED.tokens,
                    turns    = cowork_usage_daily.turns    + 1
            """), {
                "dept": current_user.get("department") or "", "uid": current_user["sub"],
                "surface": body.surface[:20], "cost": max(0.0, body.cost_usd),
                "tok": max(0, body.input_tokens) + max(0, body.output_tokens),
            })
            conn.commit()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    # Enterprise telemetry: emit an OTLP usage event (no-op unless configured).
    try:
        from core.otel import record_event
        record_event("cowork.usage", **{
            "enduser.id": current_user["sub"],
            "cowork.department": current_user.get("department") or "",
            "cowork.surface": body.surface[:20],
            "cowork.model": body.model or "",
            "cowork.cost_usd": max(0.0, body.cost_usd),
            "cowork.tokens": max(0, body.input_tokens) + max(0, body.output_tokens),
        })
    except Exception:
        pass
    return {"recorded": True}


# ── My usage ──────────────────────────────────────────────────────────────────
@router.get("/usage")
async def my_usage(current_user: dict = Depends(get_current_user)):
    # Reads the pre-aggregated daily rollup, not the raw table (scaling).
    engine, text = _db()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(tokens),0), COALESCE(SUM(turns),0)
            FROM cowork_usage_daily
            WHERE user_id = :uid AND day >= date_trunc('month', NOW())::date
        """), {"uid": current_user["sub"]}).fetchone()
    return {"month_to_date": {"cost_usd": float(row[0]), "tokens": int(row[1]), "turns": int(row[2])}}


# ── Admin analytics ───────────────────────────────────────────────────────────
@router.get("/usage/analytics")
async def usage_analytics(current_user: dict = Depends(require_admin)):
    # All aggregates come from the daily rollup — no full-table scan (scaling).
    engine, text = _db()
    with engine.connect() as conn:
        by_dept = conn.execute(text("""
            SELECT COALESCE(NULLIF(department,''),'(none)'), SUM(cost_usd), SUM(tokens), COUNT(DISTINCT user_id), SUM(turns)
            FROM cowork_usage_daily WHERE day >= date_trunc('month', NOW())::date
            GROUP BY 1 ORDER BY 2 DESC
        """)).fetchall()
        by_user = conn.execute(text("""
            SELECT user_id, COALESCE(department,''), SUM(cost_usd), SUM(turns)
            FROM cowork_usage_daily WHERE day >= date_trunc('month', NOW())::date
            GROUP BY 1,2 ORDER BY 3 DESC LIMIT 50
        """)).fetchall()
    return {
        "by_department": [{"department": r[0], "cost_usd": float(r[1]), "tokens": int(r[2]), "users": int(r[3]), "turns": int(r[4])} for r in by_dept],
        "top_users": [{"user_id": r[0], "department": r[1], "cost_usd": float(r[2]), "turns": int(r[3])} for r in by_user],
    }


# ── Group spend check (pre-spawn / pre-run guard) ─────────────────────────────
def group_spend_status(user_id: str, department: str) -> dict:
    """Returns {over, spent, limit} for the user's department this month. 0 limit = unlimited."""
    engine, text = _db()
    try:
        with engine.connect() as conn:
            lim = conn.execute(text("SELECT monthly_usd FROM cowork_spend_limits WHERE department = :d"),
                               {"d": department or ""}).fetchone()
            limit = float(lim[0]) if lim else 0.0
            spent = conn.execute(text("""
                SELECT COALESCE(SUM(cost_usd),0) FROM cowork_usage_daily
                WHERE department = :d AND day >= date_trunc('month', NOW())::date
            """), {"d": department or ""}).fetchone()[0]
        spent = float(spent)
        return {"over": (limit > 0 and spent >= limit), "spent": spent, "limit": limit}
    except Exception as e:
        logger.debug(f"cowork spend check failed → {e}")
        return {"over": False, "spent": 0.0, "limit": 0.0}


@router.get("/usage/spend")
async def my_spend(current_user: dict = Depends(get_current_user)):
    return group_spend_status(current_user["sub"], current_user.get("department") or "")


# ── Spend limits (admin) ──────────────────────────────────────────────────────
class SpendLimit(BaseModel):
    department: str
    monthly_usd: float = 0.0


@router.get("/spend-limits")
async def list_spend_limits(current_user: dict = Depends(require_admin)):
    engine, text = _db()
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT department, monthly_usd FROM cowork_spend_limits ORDER BY department")).fetchall()
    return {"limits": [{"department": r[0], "monthly_usd": float(r[1])} for r in rows]}


class CUAudit(BaseModel):
    session_id: str
    action: str
    target: Optional[str] = None
    allowed: bool = False
    block_reason: Optional[str] = None
    findings_count: int = 0
    redacted: bool = False


@router.post("/computer-use/audit", status_code=201)
async def record_computer_use(body: CUAudit, current_user: dict = Depends(get_current_user)):
    """Record one computer-use action (P4). Values are NEVER stored — only the
    event, target (app/host), allow/block, and redaction status."""
    engine, text = _db()
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO cowork_computer_use_audit
                    (user_id, department, session_id, action, target, allowed, block_reason, findings_count, redacted)
                VALUES (:uid, :dept, :sid, :action, :target, :allowed, :reason, :fc, :red)
            """), {
                "uid": current_user["sub"], "dept": current_user.get("department") or "",
                "sid": body.session_id[:120], "action": body.action[:120], "target": (body.target or "")[:255],
                "allowed": body.allowed, "reason": body.block_reason, "fc": max(0, body.findings_count),
                "red": body.redacted,
            })
            conn.commit()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    return {"recorded": True}


@router.put("/spend-limits")
async def set_spend_limit(body: SpendLimit, current_user: dict = Depends(require_admin)):
    engine, text = _db()
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO cowork_spend_limits (department, monthly_usd, updated_by, updated_at)
                VALUES (:d, :m, :by, NOW())
                ON CONFLICT (department) DO UPDATE SET monthly_usd = :m, updated_by = :by, updated_at = NOW()
            """), {"d": body.department.strip(), "m": max(0.0, body.monthly_usd), "by": current_user["sub"]})
            conn.commit()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    return {"saved": True, "department": body.department, "monthly_usd": body.monthly_usd}
