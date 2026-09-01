# SPDX-License-Identifier: Apache-2.0
"""
SCIM 2.0 provisioning endpoints (RFC 7643 / 7644).

Lets an enterprise IdP (Okta, Azure AD, Ping, OneLogin, …) provision and
de-provision AiNxt users automatically — the standard surface Claude Cowork's
enterprise tier exposes for managed-agent user lifecycle.

Scope: Users (full CRUD + active toggle) + read-only Groups (derived from
`department`). Backed by the existing `users` table (db.models.User). SCIM
users are SSO-provisioned (no local password); they authenticate via the
configured SSO provider.

SECURITY:
  - Bearer auth with a dedicated shared secret `SCIM_TOKEN` (env). If unset, the
    whole surface returns 503 — it is never open. Compared with hmac.compare_digest.
  - This is an ADMIN/automation surface; it does NOT use the user JWT path.
  - Never logs the token.
"""
import hmac
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from core.logger import logger, mask_email

router = APIRouter(prefix="/scim/v2", tags=["scim"])

_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_ENTERPRISE_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


# ── auth ──────────────────────────────────────────────────────────────────────
def _require_scim_token(authorization: Optional[str] = Header(None)) -> bool:
    token = os.getenv("SCIM_TOKEN", "")
    if not token:
        # Provisioning intentionally disabled until a token is configured.
        raise HTTPException(status_code=503, detail="SCIM provisioning is not enabled")
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented, token):
        raise HTTPException(status_code=401, detail="Invalid SCIM bearer token")
    return True


def _scim_error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"schemas": [_ERROR_SCHEMA], "detail": detail, "status": str(status)})


# ── mapping ─────────────────────────────────────────────────────────────────--
def _user_to_scim(u) -> dict:
    return {
        "schemas": [_USER_SCHEMA, _ENTERPRISE_SCHEMA],
        "id": str(u.id),
        "userName": u.email,
        "name": {"formatted": u.name or u.email},
        "displayName": u.name or u.email,
        "emails": [{"value": u.email, "primary": True, "type": "work"}],
        "active": bool(u.is_active),
        _ENTERPRISE_SCHEMA: {"department": u.department or ""},
        "meta": {
            "resourceType": "User",
            "created": u.created_at.isoformat() if getattr(u, "created_at", None) else None,
            "location": f"/scim/v2/Users/{u.id}",
        },
    }


def _extract_department(body: dict) -> str:
    ext = body.get(_ENTERPRISE_SCHEMA) or {}
    if isinstance(ext, dict) and ext.get("department"):
        return str(ext["department"])
    return ""


def _primary_email(body: dict) -> str:
    if body.get("userName"):
        return str(body["userName"]).strip()
    for e in body.get("emails") or []:
        if isinstance(e, dict) and e.get("value"):
            return str(e["value"]).strip()
    return ""


# ── discovery ───────────────────────────────────────────────────────────────--
@router.get("/ServiceProviderConfig")
async def service_provider_config():
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken", "name": "OAuth Bearer Token",
            "description": "Authentication via the SCIM_TOKEN bearer secret",
        }],
    }


@router.get("/ResourceTypes")
async def resource_types():
    return [
        {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
         "id": "User", "name": "User", "endpoint": "/Users", "schema": _USER_SCHEMA},
        {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
         "id": "Group", "name": "Group", "endpoint": "/Groups", "schema": _GROUP_SCHEMA},
    ]


# ── Users ───────────────────────────────────────────────────────────────────--
@router.get("/Users")
async def list_users(startIndex: int = 1, count: int = 100, filter: Optional[str] = None,
                     _: bool = Depends(_require_scim_token)):
    from db.database import SessionLocal
    from db.models import User
    db = SessionLocal()
    try:
        q = db.query(User)
        # Minimal SCIM filter support: userName eq "value"
        if filter and "userName" in filter and " eq " in filter:
            try:
                val = filter.split(" eq ", 1)[1].strip().strip('"')
                q = q.filter(User.email == val)
            except Exception:
                pass
        total = q.count()
        start = max(1, startIndex)
        rows = q.order_by(User.created_at).offset(start - 1).limit(max(0, count)).all()
        return {
            "schemas": [_LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start,
            "itemsPerPage": len(rows),
            "Resources": [_user_to_scim(u) for u in rows],
        }
    finally:
        db.close()


@router.get("/Users/{user_id}")
async def get_user(user_id: str, _: bool = Depends(_require_scim_token)):
    from db.database import SessionLocal
    from db.models import User
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return _scim_error(404, "User not found")
        return _user_to_scim(u)
    finally:
        db.close()


@router.post("/Users", status_code=201)
async def create_user(request: Request, _: bool = Depends(_require_scim_token)):
    from db.database import SessionLocal
    from db.models import User
    body = await request.json()
    email = _primary_email(body)
    if not email:
        return _scim_error(400, "userName/email is required")
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            # SCIM clients treat 409 as "already provisioned" — return the resource.
            if not existing.is_active:
                existing.is_active = True
                db.commit()
            return JSONResponse(status_code=409, content=_user_to_scim(existing))
        name = (body.get("name") or {}).get("formatted") or body.get("displayName") or email
        u = User(
            email=email, name=name, role="user",
            is_active=bool(body.get("active", True)),
            account_status="active",
            sso_provider=os.getenv("SSO_PROVIDER") or "scim",
            sso_subject=str(body.get("externalId") or email),
            department=_extract_department(body) or None,
            hashed_password=None,  # SSO-provisioned: no local password
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        logger.info(f"scim: provisioned user {mask_email(email)} (id={u.id})")
        return _user_to_scim(u)
    except Exception as exc:
        db.rollback()
        logger.error(f"scim: create_user failed → {exc}")
        return _scim_error(500, str(exc))
    finally:
        db.close()


@router.put("/Users/{user_id}")
async def replace_user(user_id: str, request: Request, _: bool = Depends(_require_scim_token)):
    from db.database import SessionLocal
    from db.models import User
    body = await request.json()
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return _scim_error(404, "User not found")
        email = _primary_email(body)
        if email:
            u.email = email
        nm = (body.get("name") or {}).get("formatted") or body.get("displayName")
        if nm:
            u.name = nm
        if "active" in body:
            u.is_active = bool(body["active"])
            u.account_status = "active" if u.is_active else "suspended"
        dept = _extract_department(body)
        if dept:
            u.department = dept
        db.commit()
        db.refresh(u)
        return _user_to_scim(u)
    except Exception as exc:
        db.rollback()
        return _scim_error(500, str(exc))
    finally:
        db.close()


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request, _: bool = Depends(_require_scim_token)):
    """Supports the common Okta/Azure de-/re-activation PatchOp:
       {"Operations":[{"op":"replace","value":{"active":false}}]} (and path form)."""
    from db.database import SessionLocal
    from db.models import User
    body = await request.json()
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return _scim_error(404, "User not found")
        for op in body.get("Operations") or []:
            opname = (op.get("op") or "").lower()
            if opname not in ("replace", "add"):
                continue
            path = (op.get("path") or "").lower()
            val = op.get("value")
            if path == "active":
                u.is_active = bool(val)
            elif isinstance(val, dict) and "active" in val:
                u.is_active = bool(val["active"])
            elif path == "displayname" and isinstance(val, str):
                u.name = val
            elif isinstance(val, dict) and val.get("displayName"):
                u.name = val["displayName"]
            u.account_status = "active" if u.is_active else "suspended"
        db.commit()
        db.refresh(u)
        return _user_to_scim(u)
    except Exception as exc:
        db.rollback()
        return _scim_error(500, str(exc))
    finally:
        db.close()


@router.delete("/Users/{user_id}", status_code=204)
async def delete_user(user_id: str, _: bool = Depends(_require_scim_token)):
    """SCIM delete = de-provision. We SOFT-deactivate (audit-friendly; never hard
    delete a financial-platform user record)."""
    from db.database import SessionLocal
    from db.models import User
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if u:
            u.is_active = False
            u.account_status = "suspended"
            db.commit()
            logger.info(f"scim: de-provisioned (deactivated) user id={user_id}")
        return JSONResponse(status_code=204, content=None)
    finally:
        db.close()


# ── Groups (read-only, derived from department) ───────────────────────────────
@router.get("/Groups")
async def list_groups(_: bool = Depends(_require_scim_token)):
    from db.database import SessionLocal
    from db.models import User
    from sqlalchemy import func
    db = SessionLocal()
    try:
        rows = (db.query(User.department, func.count(User.id))
                  .filter(User.department.isnot(None))
                  .group_by(User.department).all())
        groups = [{
            "schemas": [_GROUP_SCHEMA],
            "id": dept, "displayName": dept,
            "meta": {"resourceType": "Group", "location": f"/scim/v2/Groups/{dept}"},
            "members": [],  # membership inferred from User.department; not expanded
            "_count": int(n),
        } for dept, n in rows if dept]
        return {
            "schemas": [_LIST_SCHEMA],
            "totalResults": len(groups),
            "startIndex": 1,
            "itemsPerPage": len(groups),
            "Resources": groups,
        }
    finally:
        db.close()
