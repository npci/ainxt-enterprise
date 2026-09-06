# SPDX-License-Identifier: MIT
# ============================================================
# MCP MARKETPLACE ROUTER — /tools, /skills, /marketplace
# ============================================================

import json
import urllib.request
from typing import Optional, List

from core.config import RDB_REGISTRY
from core.kv import get_kv, KVError
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from core.logger import logger
from auth.dependencies import get_current_user as _get_current_user
from core.security_validation import validate_tool_register_request

router = APIRouter(tags=["marketplace"])


def _maybe_get_current_user(current_user: dict = Depends(_get_current_user)):
    return current_user

_r = None

def _get_redis():
    """Return a cached KV client for the marketplace registry (DB=3).

    Name retained for backwards compatibility; returns a KVClient,
    not a redis.Redis. Backend selected via REDIS_CLIENT_CONFIG_DB3.
    """
    global _r
    if _r is None:
        try:
            c = get_kv(RDB_REGISTRY, decode_responses=True)
            c.ping()
            _r = c
        except KVError as e:
            logger.warning(f"MarketplaceRouter: KV backend unavailable → {e}")
    return _r


# ============================================================
# PYDANTIC
# ============================================================

class ToolRegister(BaseModel):
    name: str
    description: str
    url: str
    method: str = "POST"
    tags: List[str] = []
    input_schema: Optional[dict] = None
    visibility: str = "private"
    is_critical: bool = False


# ============================================================
# HTTP TOOL FACTORY
# ============================================================

def make_http_tool_fn(url: str, method: str):
    def fn(**kwargs):
        data = json.dumps(kwargs).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method.upper(),
        )
        r = urllib.request.urlopen(req, timeout=15)
        try:
            return json.loads(r.read())
        finally:
            r.close()
    return fn


# ============================================================
# ENDPOINTS
# ============================================================

@router.post("/tools/register")
def register_tool(body: ToolRegister, current_user: dict = Depends(_maybe_get_current_user)):
    # Validate and sanitize all inputs
    is_valid, field_errors, sanitized = validate_tool_register_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    from mcp.registry import mcp_registry

    actor = (current_user or {}).get("email") or (current_user or {}).get("user_id", "unknown")

    # ── G10: persist to MCPServer table in Postgres ──────────────────────────
    try:
        from db.database import SessionLocal
        from db.models import MCPServer
        db = SessionLocal()
        try:
            existing = db.query(MCPServer).filter(MCPServer.name == sanitized["name"]).first()
            if existing:
                existing.endpoint      = sanitized["url"]
                existing.enabled       = True
                existing.status        = "PRODUCTION"
                existing.registered_by = actor
                existing.is_critical   = body.is_critical
            else:
                db.add(MCPServer(
                    name=sanitized["name"],
                    endpoint=sanitized["url"],
                    tools=[],
                    auth_config={},
                    enabled=True,
                    status="PRODUCTION",
                    created_by=actor,
                    registered_by=actor,
                    is_production=True,
                    is_critical=body.is_critical,
                ))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"MarketplaceRouter: MCPServer DB persist failed → {e}")

    # ── G10: hot-register into live ToolRegistry (no restart needed) ─────────
    tool_data = {
        "name":         sanitized["name"],
        "description":  sanitized["description"],
        "endpoint_url": sanitized["url"],
        "tags":         sanitized["tags"],
        "input_schema": body.input_schema or {},
        "author":       actor,
    }
    mcp_registry.tools.hot_register(tool_data)

    # ── Redis marketplace metadata (existing behaviour) ───────────────────────
    rc = _get_redis()
    data = {
        **body.dict(),
        "name":         sanitized["name"],
        "description":  sanitized["description"],
        "url":          sanitized["url"],
        "tags":         sanitized["tags"],
        "user_created": True,
        "status":       "PRODUCTION",
        "registered_by": actor,
    }
    if rc:
        rc.set(f"marketplace:tool:{sanitized['name']}", json.dumps(data))

    logger.info(f"MarketplaceRouter: hot-registered tool {sanitized['name']!r} (actor={actor})")
    return {"registered": True, "tool_name": sanitized["name"], "success": True, "name": sanitized["name"]}


@router.delete("/tools/{name}")
def delete_tool(name: str):
    from mcp.registry import mcp_registry

    rc = _get_redis()
    raw = rc.get(f"marketplace:tool:{name}") if rc else None
    if not raw:
        raise HTTPException(status_code=403, detail="Only user-created tools can be deleted")

    data = json.loads(raw)
    if not data.get("user_created"):
        raise HTTPException(status_code=403, detail="Only user-created tools can be deleted")

    try:
        mcp_registry.tools.unregister(name)
    except Exception:
        pass

    if rc:
        rc.delete(f"marketplace:tool:{name}")

    return {"success": True}


@router.post("/tools/{name}/enable")
def enable_tool(name: str):
    from mcp.registry import mcp_registry
    try:
        mcp_registry.tools.enable(name)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/tools/{name}/disable")
def disable_tool(name: str):
    from mcp.registry import mcp_registry
    try:
        mcp_registry.tools.disable(name)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/skills/{name}/enable")
def enable_skill(name: str):
    from mcp.registry import mcp_registry
    try:
        mcp_registry.skills.enable(name)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/skills/{name}/disable")
def disable_skill(name: str):
    from mcp.registry import mcp_registry
    try:
        mcp_registry.skills.disable(name)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/marketplace/stats")
def marketplace_stats(current_user: dict = Depends(_get_current_user)):
    # SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    # this endpoint previously had no auth dependency at all, exposing every
    # registered tool/skill's usage stats, registrant, and visibility/status
    # metadata (including private, unpublished entries) to any anonymous
    # caller.
    # Fix: added `current_user: dict = Depends(_get_current_user)` as a
    # function parameter so FastAPI rejects unauthenticated requests with
    # 401 before the handler runs. Deliberately not admin-only — any
    # authenticated user may still view it, matching the rest of the
    # marketplace UI, which already requires login. No other logic changed.
    from mcp.registry import mcp_registry
    rc = _get_redis()

    tools = mcp_registry.tools.list_all(enabled_only=False)
    skills = mcp_registry.skills.list_all(enabled_only=False)

    tool_stats = []
    for t in tools:
        stats = {}
        tool_meta = {}
        if rc:
            raw_stats = rc.hgetall(f"marketplace:stats:tool:{t.name}")
            stats = {
                "calls": int(raw_stats.get("calls", 0)),
                "errors": int(raw_stats.get("errors", 0)),
                "last_used": raw_stats.get("last_used", ""),
            }
            raw_meta = rc.get(f"marketplace:tool:{t.name}")
            if raw_meta:
                try:
                    tool_meta = json.loads(raw_meta)
                except Exception:
                    pass
        user_created = bool(rc and rc.exists(f"marketplace:tool:{t.name}"))
        tool_stats.append({
            "name":          t.name,
            "description":   t.description,
            "tags":          t.tags,
            "enabled":       t.enabled,
            "stats":         stats,
            "user_created":  user_created,
            "status":        tool_meta.get("status", "PRODUCTION"),
            "is_critical":   tool_meta.get("is_critical", False),
            "visibility":    tool_meta.get("visibility", "private" if user_created else "public"),
            "registered_by": tool_meta.get("registered_by", ""),
        })

    skill_stats = []
    for s in skills:
        skill_stats.append({
            "name": s.name,
            "description": s.description,
            "tags": s.tags,
            "tools": s.tools,
            "enabled": s.enabled,
            "examples": getattr(s, "examples", []),
        })

    return {"tools": tool_stats, "skills": skill_stats}


@router.get("/plugins/curated")
def curated_plugins(current_user: dict = Depends(_maybe_get_current_user)):
    """Curated plugin catalog produced by the external sync worker (P4).

    Reads config/curated_plugins.json (written by workers/external_sync_worker._import_plugins).
    Returns an empty list when the file is absent — i.e. when external sync is off — so the
    CLI's `/plugin` Discover section degrades to "none available" with zero behavior change.
    Metadata only; install stays user-initiated in the CLI.
    """
    import os
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "curated_plugins.json",
    )
    try:
        with open(path, encoding="utf-8") as fh:
            catalog = json.load(fh)
        plugins = catalog.get("plugins", []) if isinstance(catalog, dict) else []
    except FileNotFoundError:
        plugins = []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[marketplace] curated_plugins read failed: {e}")
        plugins = []
    return {"plugins": plugins, "count": len(plugins)}
