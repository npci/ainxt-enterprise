# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AUDIT ROUTER — /audit
#
# Read-only access to the request_audit_log table.
# Surfaced for the CLI's /audit command and for admin auditing UIs.
#
# Scope rules:
#   - Default: user sees only their OWN audit rows (filtered by JWT user_id).
#   - Admins (role=admin in JWT): see all rows.
#
# Endpoints:
#   GET /audit/logs?limit=20&q=<substring>
#   GET /audit/logs/me?limit=20             same as above but explicit
# ============================================================

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_, text as _text

from core.logger import logger
from core.pii_crypto import encrypt_pii, decrypt_pii
from auth.dependencies import get_current_user
from db.database import get_db, DB_SCHEMA
from db.models import RequestAuditLog

router = APIRouter(tags=["audit"])

def _require_admin(current_user: dict) -> None:
    if (current_user.get("role") or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

def _row_to_dict(row: RequestAuditLog) -> dict:
    """Hide the question_hash from non-admins — it's a SHA256 but still avoid leaking."""
    return {
        "ts":                 row.created_at.isoformat() if row.created_at else None,
        "request_id":         row.request_id,
        "user_id":            row.user_id,
        "user_email":         encrypt_pii(row.email),
        "department":         row.department,
        "client_source":      row.client_source,
        "endpoint":           row.endpoint,
        "model":              row.model_used,
        "tokens_in":          row.tokens_in,
        "tokens_out":         row.tokens_out,
        "cost_usd":           row.cost_usd,
        "latency_ms":         row.latency_ms,
        "cache_hit":          row.cache_hit,
        "compliance_blocked": row.compliance_blocked,
        "status":             "blocked"  if row.compliance_blocked else
                              "error"    if row.error else
                              "ok",
        "action":             row.endpoint or "?",
        "detail":             row.error or "",
    }


# ─────────────────────────────────────────────────────────────
# GET /audit/logs
# ─────────────────────────────────────────────────────────────
@router.get("/audit/logs")
def list_audit_logs(
    limit: int = 20,
    q: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Recent audit entries. Default scope: this user.
    Admins (role=admin) see all entries.
    """
    if limit < 1:
        limit = 20
    if limit > 500:
        limit = 500

    user_id  = current_user.get("sub") or current_user.get("user_id") or current_user.get("id")
    is_admin = (current_user.get("role") or "").lower() == "admin"

    qry = db.query(RequestAuditLog)

    if not is_admin and user_id:
        qry = qry.filter(RequestAuditLog.user_id == user_id)

    if q:
        like = f"%{q}%"
        qry = qry.filter(
            or_(
                RequestAuditLog.endpoint.ilike(like),
                RequestAuditLog.client_source.ilike(like),
                RequestAuditLog.model_used.ilike(like),
                RequestAuditLog.error.ilike(like),
                RequestAuditLog.email.ilike(like),
            )
        )

    rows = qry.order_by(desc(RequestAuditLog.created_at)).limit(limit).all()

    return {
        "entries": [_row_to_dict(r) for r in rows],
        "scope":   "admin (all users)" if is_admin else f"user {user_id}",
        "count":   len(rows),
    }


# ─────────────────────────────────────────────────────────────
# GET /audit/logs/me
# Explicit self-scope — same as the default behavior but useful
# for admins who want their own activity.
# ─────────────────────────────────────────────────────────────
@router.get("/audit/logs/me")
def list_my_audit_logs(
    limit: int = 20,
    q: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = current_user.get("sub") or current_user.get("user_id") or current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="Could not resolve user_id from JWT")

    if limit < 1:
        limit = 20
    if limit > 500:
        limit = 500

    qry = db.query(RequestAuditLog).filter(RequestAuditLog.user_id == user_id)
    if q:
        like = f"%{q}%"
        qry = qry.filter(
            or_(
                RequestAuditLog.endpoint.ilike(like),
                RequestAuditLog.client_source.ilike(like),
                RequestAuditLog.model_used.ilike(like),
                RequestAuditLog.error.ilike(like),
            )
        )

    rows = qry.order_by(desc(RequestAuditLog.created_at)).limit(limit).all()
    return {
        "entries": [_row_to_dict(r) for r in rows],
        "scope":   f"user {user_id}",
        "count":   len(rows),
    }

# ─────────────────────────────────────────────────────────────
# GET /audit/graph — tamper-evident Teams/Office boundary log (§7.4)
# Admin-only. The log stores ONLY SHA-256 hashes + non-sensitive
# counters (never raw transcripts/summaries/prompts), so surfacing
# it does not leak content across the boundary.
# ─────────────────────────────────────────────────────────────
@router.get("/audit/graph")
def list_graph_audit(
        limit: int = 50,
        stream: Optional[str] = None,
        event: Optional[str] = None,
        current_user: dict = Depends(get_current_user),
        db: Session = Depends(get_db),
):
    _require_admin(current_user)
    limit = max(1, min(limit, 500))

    where, params = [], {"lim": limit}
    if stream:
        where.append("stream = :stream")
        params["stream"] = stream
    if event:
        where.append("event = :event")
        params["event"] = event
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.execute(
        _text(
            f"SELECT stream, seq, event, user_id, resource, data_hash, meta, "
            f"       prev_hash, signature, created_at "
            f"FROM {DB_SCHEMA}.graph_audit_log{clause} "
            f"ORDER BY created_at DESC LIMIT :lim"
        ),
        params,
    ).mappings().all()

    return {
        "entries": [
            {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
            for r in rows
        ],
        "count": len(rows),
    }


# ─────────────────────────────────────────────────────────────
# GET /audit/graph/verify?stream=... — verify a stream's HMAC
# signatures AND prev_hash hash-chain. Detects any tamper/deletion.
# ─────────────────────────────────────────────────────────────
@router.get("/audit/graph/verify")
def verify_graph_audit(
        stream: str,
        current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    if not stream:
        raise HTTPException(status_code=400, detail="stream is required")
    from core.graph_audit import verify_stream
    return {"stream": stream, **verify_stream(stream)}