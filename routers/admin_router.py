# SPDX-License-Identifier: MIT
# ============================================================
# ADMIN ROUTER — org-tree sync + platform ops
# POST /admin/sync/org-tree  — upload CSV → reload org_tree → update users
# GET  /admin/sync/org-tree/status  — row count + last sync time
# ============================================================
import csv
import io
import os
from datetime import datetime
from fastapi import APIRouter, Depends, Header, HTTPException, Request, UploadFile, File
from auth.rbac import require_admin_flag
from core.logger import logger
from core.file_validator import validate_upload
from core.rate_limiter import enforce_rate_limit_with_behaviour, SENSITIVE_ADMIN

admin_router = APIRouter(prefix="/admin", tags=["admin"])

_CSV_ALLOWED_EXTENSIONS = frozenset({"csv", "txt"})
_CSV_MAX_SIZE_BYTES = 10 * 1024 * 1024   # 10 MB


def _require_sync_token(x_sync_token: str = Header(default="")):
    """
    Validate the shared-secret sync token.

    SECURITY: This is the SECOND authentication factor for /admin/sync/org-tree.
    The FIRST factor is a valid admin JWT (require_admin_flag dependency).
    Both must pass — a compromised network that can spoof IPs still cannot
    invoke this endpoint without both a signed admin JWT and ORG_SYNC_TOKEN.

    Set ORG_SYNC_TOKEN in .env; pass as -H 'X-Sync-Token: <value>' in curl.

    DAST fix: "The application relies solely on IP-based access controls for
    protecting sensitive functionalities without additional authentication
    measure."  This endpoint now requires:
      1. A signed, non-expired, admin-role JWT (get_current_user + require_admin_flag)
      2. A server-side pre-shared secret header (X-Sync-Token / ORG_SYNC_TOKEN)
    IP-based network controls remain as a defence-in-depth layer but are no
    longer the *sole* protection.
    """
    expected = os.getenv("ORG_SYNC_TOKEN", "")
    if not expected:
        raise HTTPException(503, "ORG_SYNC_TOKEN not configured on server")
    if x_sync_token != expected:
        raise HTTPException(401, "Invalid sync token")


@admin_router.post("/sync/org-tree")
async def sync_org_tree(
    request: Request,
    file: UploadFile = File(...),
    # DAST fix: require an authenticated admin JWT IN ADDITION TO the sync token.
    # Previously the endpoint only checked X-Sync-Token (shared secret = IP-like control).
    # Now the caller must hold a valid, signed admin-role JWT — a compromised network
    # or stolen sync token alone is insufficient.
    _admin=Depends(require_admin_flag),
    _token=Depends(_require_sync_token),
):
    """Upload org hierarchy CSV → TRUNCATE org_tree → bulk INSERT → UPDATE users.

    CSV columns (in any order): level, node_id, parent_id, path, dn, department,
    description, direct_reports, display_name, mail, manager, mobile, title, company
    """
    # ── Rate limit: 50 admin actions per minute per IP (behaviour-aware) ──────
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    from db.database import SessionLocal
    from db.models import OrgTree, User
    from sqlalchemy import text as _text

    content = await file.read()

    # ── Security: validate that this is really a plain-text CSV ────────────
    vr = validate_upload(
        filename=file.filename or "org_tree.csv",
        content=content,
        allowed_extensions=_CSV_ALLOWED_EXTENSIONS,
        max_size_bytes=_CSV_MAX_SIZE_BYTES,
        caller="admin_router/sync-org-tree",
    )
    if not vr.valid:
        logger.warning(
            f"admin_router: org-tree upload rejected '{file.filename}': {vr.error}"
        )
        raise HTTPException(status_code=415, detail=vr.error or "Only CSV files are accepted")

    text    = content.decode("utf-8-sig")   # handle BOM
    reader  = csv.DictReader(io.StringIO(text))
    rows    = list(reader)

    if not rows:
        raise HTTPException(400, "CSV is empty")

    def _norm(row: dict) -> dict:
        """Normalise all CSV column names to lowercase+stripped so AD exports match regardless of casing."""
        return {k.lower().strip().replace(" ", "_"): (v.strip() if isinstance(v, str) else v)
                for k, v in row.items()}

    def _get(row: dict, *keys, default="") -> str:
        for k in keys:
            v = row.get(k.lower(), "")
            if v:
                return v
        return default

    def _direct_reports_text(value: str) -> str | None:
        """Store direct_reports as-is (DN list or names, semicolon-separated).
        Normalise separators to '; ' for consistency. Returns None if empty."""
        if not value or not value.strip():
            return None
        # Normalise: split on ; or newline, rejoin with '; '
        parts = [p.strip() for p in value.replace("\n", ";").split(";") if p.strip()]
        return "; ".join(parts) if parts else None

    rows = [_norm(r) for r in rows]

    db = SessionLocal()
    try:
        # Flash-reload org_tree
        db.execute(_text("TRUNCATE TABLE org_tree RESTART IDENTITY"))

        inserted = 0
        errors   = []
        for i, row in enumerate(rows):
            try:
                ot = OrgTree(
                    level          = int(_get(row, "level") or 6),
                    node_id        = _get(row, "node_id", "id", "objectguid", "objectid", "cn") or None,
                    parent_id      = _get(row, "parent_id", "parent", "parentid") or None,
                    path           = _get(row, "path") or None,
                    dn             = _get(row, "dn", "distinguishedname") or None,
                    department     = _get(row, "department") or None,
                    description    = _get(row, "description") or None,
                    direct_reports = _direct_reports_text(_get(row, "direct_reports", "directreports")),
                    display_name   = _get(row, "display_name", "displayname", "name") or "",
                    mail           = (_get(row, "mail", "email", "userprincipalname") or "").lower() or None,
                    manager        = _get(row, "manager") or None,
                    mobile         = _get(row, "mobile", "telephonenumber", "phone") or None,
                    title          = _get(row, "title") or None,
                    company        = _get(row, "company") or None,
                    synced_at      = datetime.utcnow(),
                )
                db.add(ot)
                inserted += 1
            except Exception as e:
                msg = f"row {i+2}: {e}"
                logger.warning(f"org_tree row skip: {msg} — row: {row}")
                errors.append(msg)

        db.flush()

        # Propagate to users — match on email (case-insensitive)
        updated  = 0
        org_map  = {
            ot.mail: ot
            for ot in db.query(OrgTree).filter(OrgTree.mail.isnot(None)).all()
        }
        users = db.query(User).all()
        for u in users:
            ot = org_map.get((u.email or "").lower())
            if ot:
                u.ad_level     = ot.level
                u.department   = ot.department
                u.ad_title     = ot.title
                u.manager_dn   = ot.manager
                u.last_ad_sync = datetime.utcnow()
                updated += 1

        db.commit()
        logger.info(f"org_tree sync: {inserted} rows inserted, {updated} users updated")
        return {
            "rows_inserted":  inserted,
            "users_updated":  updated,
            "errors":         errors[:50],   # cap error list at 50 for readability
        }

    except Exception as e:
        db.rollback()
        logger.error(f"org_tree sync failed: {e}")
        raise HTTPException(500, f"Sync failed: {e}")
    finally:
        db.close()


@admin_router.get("/sync/org-tree/status")
def org_tree_status(_caller=Depends(require_admin_flag)):
    """Return last sync time and row count."""
    from db.database import SessionLocal
    from db.models import OrgTree

    db = SessionLocal()
    try:
        count  = db.query(OrgTree).count()
        latest = db.query(OrgTree).order_by(OrgTree.synced_at.desc()).first()
        return {
            "row_count":   count,
            "last_synced": latest.synced_at.isoformat() if latest else None,
        }
    finally:
        db.close()


@admin_router.get("/models")
def list_local_models(_caller=Depends(require_admin_flag)):
    """
    Return the live model catalog from the in-house Local LLM proxy.
    Shows all discovered models, their tier assignments, and which model
    would be selected for each tier right now.
    """
    try:
        from gateway_local_llm import get_local_gateway, _catalog, LOCAL_LLM_BASE_URL
        gw = get_local_gateway()
        return {
            "local_llm_base_url": LOCAL_LLM_BASE_URL or "(not configured)",
            "available":          gw.available,
            "models":             gw.list_models(),
            "by_tier":            gw.models_by_tier(),
            "selected": {
                "simple":  _catalog.pick("simple"),
                "medium":  _catalog.pick("medium"),
                "complex": _catalog.pick("complex"),
            },
        }
    except Exception as e:
        return {"error": str(e), "local_llm_base_url": "(not configured)"}


@admin_router.get("/circuit-breakers")
def get_circuit_breakers(_caller=Depends(require_admin_flag)):
    """Return state of all circuit breakers."""
    from core.circuit_breaker import all_breaker_states
    return {"breakers": all_breaker_states()}


@admin_router.post("/circuit-breakers/{name}/reset")
def reset_circuit_breaker(name: str, _caller=Depends(require_admin_flag)):
    """Force-reset a named circuit breaker to CLOSED state."""
    from core.circuit_breaker import get_breaker
    try:
        breaker = get_breaker(name)
        breaker._set_state("CLOSED")
        breaker._reset_failures()
        logger.info(f"Circuit breaker [{name}] manually reset to CLOSED by admin")
        return {"name": name, "state": "CLOSED", "reset": True}
    except Exception as e:
        raise HTTPException(500, f"Reset failed: {e}")


@admin_router.post("/circuit-breakers/reset-all")
def reset_all_circuit_breakers(_caller=Depends(require_admin_flag)):
    """Force-reset ALL circuit breakers to CLOSED state."""
    from core.circuit_breaker import all_breaker_states, get_breaker, _breakers
    reset = []
    for name in list(_breakers.keys()):
        try:
            b = _breakers[name]
            b._set_state("CLOSED")
            b._reset_failures()
            reset.append(name)
        except Exception:
            pass
    logger.info(f"All circuit breakers reset by admin: {reset}")
    return {"reset": reset}


@admin_router.get("/capabilities")
def get_capabilities(_caller=Depends(require_admin_flag)):
    """Read-only capability catalog: every registered tool and skill.

    Exposes the existing MCPRegistry.describe() catalogue over HTTP. Adds
    zero new state and changes zero existing behaviour — it is a read-only
    view over the same registry objects that ConnectorRegistry/ToolRegistry/
    SkillRegistry already populate at startup and that MCP already serves
    internally.
    """
    from mcp.registry import mcp_registry
    return mcp_registry.describe()


# ============================================================
# COMPLIANCE CONFIG MANAGEMENT
# GET  /admin/compliance/config          — view current config
# PATCH /admin/compliance/config         — update per-type settings
# POST /admin/compliance/config/reload   — re-read config file from disk
# POST /admin/compliance/config/reset    — reset to all-redact defaults
#
# action values: "redact" | "block" | "off"
# enabled:       true | false
#
# Example PATCH body:
#   {"types": {"EMAIL": {"enabled": false}, "PAN": {"action": "block"}}}
# ============================================================

from pydantic import BaseModel
from typing import Optional

class _TypePatch(BaseModel):
    enabled: Optional[bool] = None
    action:  Optional[str]  = None   # "redact" | "block" | "off"

class _ConfigPatch(BaseModel):
    types: dict[str, _TypePatch] = {}


@admin_router.get("/compliance/config")
def get_compliance_config(_caller=Depends(require_admin_flag)):
    from agents.compliance_engine import compliance_engine
    cfg = compliance_engine.get_config()
    types = cfg.get("types", {})
    summary = {
        "redact": [t for t, v in types.items() if v.get("enabled") and v.get("action") == "redact"],
        "block":  [t for t, v in types.items() if v.get("enabled") and v.get("action") == "block"],
        "off":    [t for t, v in types.items() if not v.get("enabled") or v.get("action") == "off"],
    }
    return {"config": cfg, "summary": summary}


@admin_router.patch("/compliance/config")
def patch_compliance_config(body: _ConfigPatch, _caller=Depends(require_admin_flag)):
    from agents.compliance_engine import compliance_engine
    try:
        patch = {"types": {t: v.model_dump(exclude_none=True) for t, v in body.types.items()}}
        new_cfg = compliance_engine.update_config(patch)
        logger.info(f"Compliance config updated by admin")
        return {"updated": True, "config": new_cfg}
    except ValueError as e:
        raise HTTPException(400, str(e))


@admin_router.post("/compliance/config/reload")
def reload_compliance_config(_caller=Depends(require_admin_flag)):
    from agents.compliance_engine import compliance_engine
    cfg = compliance_engine.reload_config()
    logger.info("Compliance config reloaded from disk by admin")
    return {"reloaded": True, "config": cfg}


@admin_router.post("/compliance/config/reset")
def reset_compliance_config(_caller=Depends(require_admin_flag)):
    """Reset all types to enabled=true, action=redact."""
    from agents.compliance_engine import compliance_engine, _DEFAULT_TYPES
    patch = {"types": {t: {"enabled": True, "action": "redact"} for t in _DEFAULT_TYPES}}
    new_cfg = compliance_engine.update_config(patch)
    logger.info("Compliance config reset to defaults by admin")
    return {"reset": True, "config": new_cfg}
