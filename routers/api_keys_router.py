# SPDX-License-Identifier: Apache-2.0
# ============================================================
# API KEYS ROUTER — per-user IDE API key management
#
# Endpoints (prefix: /ainxt/v1/api):
#   GET    /profile/api-keys          — list own keys (masked)
#   POST   /profile/api-keys          — generate a new key (plaintext returned ONCE)
#   DELETE /profile/api-keys/{key_id} — revoke a key
#
# Management endpoints require JWT auth (browser login).
# A compromised API key cannot be used to generate more keys.
# ============================================================

import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_

from auth.dependencies import get_current_user
from core.security_validation import validate_identifier, _flatten_errors

router = APIRouter(prefix="/profile/api-keys", tags=["api-keys"])


# ── NULL-safe label filters ───────────────────────────────────────────────────
#
# A bare ``~UserAPIKey.label.like("endpoint:%")`` looks correct but silently
# DROPS every row whose label is NULL: in SQL, ``NULL NOT LIKE 'x'`` evaluates
# to NULL, and ``WHERE NULL`` discards the row. Because the label used to be
# optional, most historical keys have label = NULL — so those keys became
# invisible in the profile list and were excluded from the per-user cap, even
# though they authenticate normally. The owner could not see or revoke them.
#
# These helpers make the intent explicit: "a key with no label is NOT an
# endpoint key and NOT a device key", which is true.


def _not_endpoint_key():
    """Match keys that are not managed-endpoint keys (NULL label included)."""
    from db.models import UserAPIKey
    return or_(UserAPIKey.label.is_(None), ~UserAPIKey.label.like("endpoint:%"))


def _not_device_key():
    """Match keys that are not self-recycling desktop keys (NULL label included)."""
    from db.models import UserAPIKey
    return or_(UserAPIKey.label.is_(None), ~UserAPIKey.label.like("desktop:%"))


_MAX_KEYS_PER_USER = 5
_KEY_LIFETIME_DAYS = int(os.getenv("API_KEY_LIFETIME_DAYS", "180"))  # 6 months default


# ── Pydantic models ────────────────────────────────────────────────────────────

class APIKeyOut(BaseModel):
    id:             str
    key_prefix:     str
    label:          Optional[str]
    last_used_at:   Optional[str]
    created_at:     str
    expires_at:     Optional[str]
    is_expiring_soon: bool
    is_active:      bool


class APIKeyCreateRequest(BaseModel):
    # A label is MANDATORY (infosec: unlabelled keys are unauditable — nobody can
    # tell what a key is for, which device holds it, or whether it is safe to
    # revoke). Enforced server-side, not just in the UI, because the endpoint is
    # callable directly.
    #
    # min_length=1 rejects "" outright; the validator below also rejects
    # whitespace-only values and normalises the stored value.
    label: str = Field(..., min_length=1, max_length=64)

    @field_validator("label")
    @classmethod
    def _label_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Label is required — give the key a recognisable name.")
        return v


class APIKeyCreateResponse(BaseModel):
    id:         str
    key:        str           # full raw key — shown ONCE, never stored
    key_prefix: str
    label:      Optional[str]
    created_at: str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _slugify(text: str, max_len: int = 20) -> str:
    """Convert email/name to safe slug for key prefix: lowercase alphanumeric + hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "user"


def mint_api_key_for_user(user_id: str, email: str = "", label: Optional[str] = None) -> str:
    """Create and persist a new API key for a user; return the RAW key (once).

    Reusable helper (used by the SSO desktop-exchange flow) so a long-lived CLI
    credential can be issued programmatically. If the user is at the key cap, the
    oldest non-endpoint key is revoked to make room (desktop keys are single-per
    -device and self-heal). Returns "" on failure.
    """
    from db.database import SessionLocal
    from db.models import UserAPIKey

    slug = _slugify(email.split("@")[0] if "@" in email else (email or "user"))
    db = SessionLocal()
    try:
        active = (
            db.query(UserAPIKey)
            .filter(UserAPIKey.user_id == user_id,
                    UserAPIKey.is_active == True,          # noqa: E712
                    _not_endpoint_key())
            .order_by(UserAPIKey.created_at.asc())
            .all()
        )
        # Reuse-by-revoke: retire any same-label (device) key so we don't
        # accumulate, then enforce the global cap — all in ONE transaction.
        for row in active:
            if label and row.label == label:
                row.is_active = False
        active = [r for r in active if r.is_active]
        while len(active) >= _MAX_KEYS_PER_USER:
            active.pop(0).is_active = False

        raw_key    = f"{slug}-{uuid.uuid4()}"
        short_uuid = raw_key[len(slug) + 1: len(slug) + 9]
        now = datetime.utcnow()
        db.add(UserAPIKey(
            user_id=user_id, key_prefix=f"{slug}-{short_uuid}", key_hash=_sha256_hex(raw_key),
            label=(label.strip() if label else None), is_active=True,
            created_at=now,
            expires_at=now + timedelta(days=_KEY_LIFETIME_DAYS),
        ))
        db.commit()
        return raw_key
    except Exception:
        db.rollback()
        return ""
    finally:
        db.close()


def _fmt(dt) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


_EXPIRING_SOON_DAYS = int(os.getenv("API_KEY_EXPIRING_SOON_DAYS", "15"))


def _is_expiring_soon(expires_at) -> bool:
    if expires_at is None:
        return False
    remaining = (expires_at - datetime.utcnow()).total_seconds()
    return 0 < remaining <= _EXPIRING_SOON_DAYS * 86400


def _require_jwt_auth(current_user: dict = Depends(get_current_user)) -> dict:
    """
    Key management must be done via browser session (JWT), not via an API key itself.
    This prevents a leaked API key from being used to spawn more keys.
    """
    if current_user.get("auth_method") == "api_key":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key management requires browser login (JWT session)",
        )
    return current_user


# ── GET /profile/api-keys ──────────────────────────────────────────────────────

@router.get("", response_model=list[APIKeyOut])
def list_api_keys(current_user: dict = Depends(_require_jwt_auth)):
    """Return all API keys for the current user (masked — never the raw key)."""
    from db.database import SessionLocal
    from db.models import UserAPIKey

    user_id = current_user["sub"]
    db = SessionLocal()
    try:
        rows = (
            db.query(UserAPIKey)
            .filter(
                UserAPIKey.user_id == user_id,
                _not_endpoint_key(),   # exclude managed-endpoint keys (NULL-safe)
            )
            .order_by(UserAPIKey.created_at.desc())
            .all()
        )
        return [
            APIKeyOut(
                id=str(r.id),
                key_prefix=r.key_prefix,
                label=r.label,
                last_used_at=_fmt(r.last_used_at),
                created_at=_fmt(r.created_at),
                expires_at=_fmt(r.expires_at),
                is_expiring_soon=_is_expiring_soon(r.expires_at),
                is_active=r.is_active,
            )
            for r in rows
        ]
    finally:
        db.close()


# ── POST /profile/api-keys ─────────────────────────────────────────────────────

@router.post("", response_model=APIKeyCreateResponse, status_code=status.HTTP_201_CREATED)
def create_api_key(
    body: APIKeyCreateRequest,
    current_user: dict = Depends(_require_jwt_auth),
):
    """
    Generate a new IDE API key.
    The raw key is returned ONCE in this response — it is never stored or returned again.
    """
    from db.database import SessionLocal
    from db.models import UserAPIKey

    user_id = current_user["sub"]
    email   = current_user.get("email", "")
    slug    = _slugify(email.split("@")[0] if "@" in email else email)

    # Already validated non-blank and stripped by APIKeyCreateRequest.
    label = body.label.strip()
    _ok_label, _errs_label, _san_label = validate_identifier(label)
    if not _ok_label:
        raise HTTPException(status_code=400, detail=_flatten_errors({"label": _errs_label}))
    label = _san_label or label
    # A desktop mint (label "desktop:*") is a DEVICE credential: the app only ever
    # needs ONE live key and re-mints it on install / config.json loss / user switch.
    # Without recycling, every mount added a new key until the 5-cap was hit and the
    # endpoint returned 409 forever — the "Sign in with CLI" failure users saw. So for
    # device mints we revoke ALL existing active desktop:* keys first (labels differ by
    # source — navigator.platform vs hostname — so match the prefix, not the exact label)
    # and exclude device keys from the cap. User-created keys keep the hard 5-cap.
    is_device_key = bool(label and label.startswith("desktop:"))

    db = SessionLocal()
    try:
        if is_device_key:
            stale_devices = (
                db.query(UserAPIKey)
                .filter(
                    UserAPIKey.user_id == user_id,
                    UserAPIKey.is_active == True,          # noqa: E712
                    UserAPIKey.label.like("desktop:%"),
                )
                .all()
            )
            for row in stale_devices:
                row.is_active = False
                row.revoked_at = datetime.utcnow()

        # Enforce per-user key cap (endpoint AND device keys are excluded from the count)
        active_count = (
            db.query(UserAPIKey)
            .filter(
                UserAPIKey.user_id == user_id,
                UserAPIKey.is_active == True,          # noqa: E712
                _not_endpoint_key(),   # exclude managed-endpoint keys (NULL-safe)
                _not_device_key(),     # exclude self-recycling device keys (NULL-safe)
            )
            .count()
        )
        if not is_device_key and active_count >= _MAX_KEYS_PER_USER:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Maximum of {_MAX_KEYS_PER_USER} active API keys allowed. Revoke an existing key first.",
            )

        # Generate raw key:  {slug}-{uuid4}
        raw_key    = f"{slug}-{uuid.uuid4()}"
        key_hash   = _sha256_hex(raw_key)
        # Prefix = first token (slug) + first 8 chars of UUID
        short_uuid = raw_key[len(slug) + 1: len(slug) + 9]
        key_prefix = f"{slug}-{short_uuid}"

        now = datetime.utcnow()
        row = UserAPIKey(
            user_id    = user_id,
            key_prefix = key_prefix,
            key_hash   = key_hash,
            label      = label,
            is_active  = True,
            created_at = now,
            expires_at = now + timedelta(days=_KEY_LIFETIME_DAYS),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        return APIKeyCreateResponse(
            id         = str(row.id),
            key        = raw_key,       # shown ONCE — user must copy it now
            key_prefix = key_prefix,
            label      = row.label,
            created_at = _fmt(row.created_at),
        )
    finally:
        db.close()


# ── DELETE /profile/api-keys/{key_id} ─────────────────────────────────────────

@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(
    key_id: str,
    current_user: dict = Depends(_require_jwt_auth),
):
    """Revoke an API key. The IDE using it will be rejected immediately."""
    from db.database import SessionLocal
    from db.models import UserAPIKey

    user_id = current_user["sub"]
    db = SessionLocal()
    try:
        row = (
            db.query(UserAPIKey)
            .filter(UserAPIKey.id == key_id, UserAPIKey.user_id == user_id)
            .first()
        )
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
        if row.label and row.label.startswith("endpoint:"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Endpoint API keys cannot be revoked from the profile page. "
                    "Use the Endpoint Management page to regenerate or delete the endpoint."
                ),
            )
        if not row.is_active:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="API key already revoked")

        row.is_active   = False
        row.revoked_at  = datetime.utcnow()
        db.commit()
    finally:
        db.close()