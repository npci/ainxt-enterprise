# SPDX-License-Identifier: MIT
# ============================================================
# VAULT ROUTER — /vault
# Encrypted credential management backed by store/credential_vault.py
#
# Write endpoints:  require admin role
# Read  endpoints:  require authenticated user (get_current_user)
# Value endpoint:   require admin + emits audit log
#
# Tags: ["vault", "security"]
# ============================================================

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from auth.dependencies import get_current_user, require_admin

router = APIRouter(prefix="/vault", tags=["vault", "security"])

# Available credential categories
_CATEGORIES = ["api_key", "oauth_token", "password", "certificate"]


# ============================================================
# REQUEST / RESPONSE MODELS
# ============================================================

class CredentialCreate(BaseModel):
    name:        str
    value:       str
    description: Optional[str] = None
    category:    str = "api_key"
    tags:        List[str] = []


class CredentialUpdate(BaseModel):
    value:       Optional[str] = None
    description: Optional[str] = None
    tags:        Optional[List[str]] = None


class RotateRequest(BaseModel):
    new_value: str


# ============================================================
# POST /vault — create credential
# ============================================================

@router.post("", status_code=201)
def create_credential(
    body: CredentialCreate,
    admin: dict = Depends(require_admin),
):
    """Create a new encrypted credential.
    The plaintext *value* is encrypted with AES-256-GCM before storage
    (see SEC-F-020/032 in store/credential_vault.py).
    Returns credential metadata — never returns the encrypted or plaintext value.
    """
    from store.credential_vault import create_credential as _create

    owner_id = admin.get("sub") or None
    try:
        result = _create(
            name=body.name,
            value=body.value,
            description=body.description,
            category=body.category,
            tags=body.tags,
            owner_id=owner_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create credential: {exc}",
        )
    return {"success": True, "credential": result}


# ============================================================
# GET /vault — list credentials (no values)
# ============================================================

@router.get("")
def list_credentials(
    category: Optional[str] = Query(default=None, description="Filter by category"),
    current_user: dict = Depends(get_current_user),
):
    """List all credential metadata records.  Values are never returned.
    Optionally filter by *category* (api_key | oauth_token | password | certificate).
    """
    from store.credential_vault import list_credentials as _list

    if category and category not in _CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown category '{category}'. Valid values: {_CATEGORIES}",
        )
    credentials = _list(category=category)
    return {"credentials": credentials, "count": len(credentials)}


# ============================================================
# GET /vault/categories — list available categories
# NOTE: must be registered BEFORE /vault/{name} to avoid route shadowing.
# ============================================================

@router.get("/categories")
def list_categories(current_user: dict = Depends(get_current_user)):
    """Return the list of valid credential categories."""
    return {"categories": _CATEGORIES}


# ============================================================
# GET /vault/{name} — get credential metadata (no value)
# ============================================================

@router.get("/{name}")
def get_credential(
    name: str,
    current_user: dict = Depends(get_current_user),
):
    """Return metadata for a named credential.  Does NOT return the decrypted value."""
    from store.credential_vault import get_credential as _get

    record = _get(name)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credential '{name}' not found",
        )
    return {"credential": record}


# ============================================================
# PUT /vault/{name} — update credential
# ============================================================

@router.put("/{name}")
def update_credential(
    name: str,
    body: CredentialUpdate,
    admin: dict = Depends(require_admin),
):
    """Update an existing credential.  *value*, *description*, and *tags* are
    each optional — only supplied fields are changed.
    """
    from store.credential_vault import update_credential as _update

    result = _update(
        name=name,
        value=body.value,
        description=body.description,
        tags=body.tags,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credential '{name}' not found",
        )
    return {"success": True, "credential": result}


# ============================================================
# DELETE /vault/{name} — delete credential
# ============================================================

@router.delete("/{name}")
def delete_credential(
    name: str,
    admin: dict = Depends(require_admin),
):
    """Permanently delete a credential from the vault."""
    from store.credential_vault import delete_credential as _delete

    deleted = _delete(name)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credential '{name}' not found",
        )
    return {"success": True, "name": name}


# ============================================================
# POST /vault/{name}/rotate — rotate credential value
# ============================================================

@router.post("/{name}/rotate")
def rotate_credential(
    name: str,
    body: RotateRequest,
    admin: dict = Depends(require_admin),
):
    """Replace the stored value with *new_value* and stamp *last_rotated*.
    The old ciphertext is overwritten atomically.
    """
    from store.credential_vault import rotate_credential as _rotate

    result = _rotate(name, body.new_value)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credential '{name}' not found",
        )
    return {"success": True, "credential": result}


# ============================================================
# GET /vault/{name}/value — get decrypted value (admin only, audit logged)
# ============================================================

@router.get("/{name}/value")
def get_credential_value(
    name: str,
    admin: dict = Depends(require_admin),
):
    """Return the *decrypted* plaintext value for a named credential.
    Restricted to admin users only.  Every call is audit-logged.
    """
    from store.credential_vault import get_credential_value as _get_value
    from core.logger import logger

    # Audit log — who accessed what and when
    accessor = admin.get("email") or admin.get("sub") or "unknown"
    logger.info(
        f"CredentialVault AUDIT: value access — credential='{name}' "
        f"by user='{accessor}' role='admin'"
    )

    plaintext = _get_value(name)
    if plaintext is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credential '{name}' not found or vault unavailable",
        )
    return {"name": name, "value": plaintext}
