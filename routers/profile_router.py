#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ============================================================
# PROFILE ROUTER — user profile + API token management
#
# Endpoints:
#   GET  /profile                 — get own profile + ABAC fields
#   PUT  /profile                 — update name, gitlab_username
#   GET  /profile/tokens          — list own API tokens (masked)
#   POST /profile/tokens          — add/update an API token
#   DELETE /profile/tokens/{type} — remove a token
#
# Token types: local_llm | atlassian | gitlab | github
# Values stored AES-256-GCM-encrypted (SEC-F-020/032 follow-up, 2026-08-26 —
# migrated off Fernet/AES-128-CBC the same way store/credential_vault.py was).
# Only masked preview returned to client.
# ============================================================

import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.logger import logger, mask_email
from core.pii_crypto import encrypt_pii, decrypt_pii, looks_encrypted
from core.security_validation import validate_profile_update_request, validate_token_upsert_request

router = APIRouter(prefix="/profile", tags=["profile"])

TOKEN_TYPES = {"local_llm", "atlassian", "gitlab", "github"}

# ── At-rest encryption for user_tokens.encrypted_value ───────────────────────
# SEC-F-020/032 follow-up: this used to build its own Fernet instance directly
# from FERNET_KEY. It now delegates to store/credential_vault.py's
# encrypt_value()/decrypt_value(), which use AES-256-GCM for new values and
# still transparently decrypt legacy (unprefixed) Fernet tokens written
# before this migration — the SAME FERNET_KEY/VAULT_ENCRYPTION_KEY env var is
# reused as the AES-256-GCM key, so no new secret and no key rotation is
# needed. Centralising here (rather than a second independent AES-GCM
# implementation) means both stores share one crypto code path to keep in
# sync and test.
def _encrypt(value: str) -> str:
    try:
        from store.credential_vault import encrypt_value
        return encrypt_value(value)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Encryption service unavailable: {exc}",
        )


def _decrypt(encrypted: str) -> str:
    try:
        from store.credential_vault import decrypt_value
        return decrypt_value(encrypted)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Encryption service unavailable: {exc}",
        )


def _decrypt_transport(value: str) -> str:
    """AES-GCM transport-decrypt a token encrypted by the frontend.
    Payload format: base64(iv[12] || ciphertext).
    Falls back to value as-is if LOGIN_ENCRYPT_KEY is absent or decryption fails.

    NOTE: this is a SEPARATE, one-time transport-layer encryption (frontend →
    backend, keyed by LOGIN_ENCRYPT_KEY) that happens BEFORE _encrypt() stores
    the value at rest — not the at-rest scheme itself. Unrelated to the
    Fernet→AES-256-GCM migration above."""
    import base64
    key_b64 = os.getenv("LOGIN_ENCRYPT_KEY", "")
    if not key_b64:
        return value
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        combined = base64.b64decode(value)
        iv, ciphertext = combined[:12], combined[12:]
        return AESGCM(base64.b64decode(key_b64)).decrypt(iv, ciphertext, None).decode()
    except Exception:
        return value  # not encrypted or wrong key — pass through


def _mask(value: str) -> str:
    """Return masked token preview: first 4 + last 4 chars."""
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


# ── DB helpers ────────────────────────────────────────────────────────────────
def _db():
    from db.database import engine
    from sqlalchemy import text
    return engine, text


# ── Schemas ───────────────────────────────────────────────────────────────────
class ProfileUpdate(BaseModel):
    name:            Optional[str] = None
    gitlab_username: Optional[str] = None


class TokenUpsert(BaseModel):
    # "github" is a genuinely used token_type: core/platform_credentials.py's
    # get_github_token()/inject_github_token() read/write user_tokens rows
    # with token_type="github" whenever SCM_PROVIDER=github (the OSS
    # default), and ai-ui/src/components/Profile.jsx's buildTokenTypes()
    # renders a GitHub PAT field and POSTs token_type="github" for exactly
    # that case. This Literal previously omitted "github", so every save
    # attempt failed 422 with "token_type: Input should be 'local_llm',
    # 'atlassian' or 'gitlab'" for any GitHub-provider deployment.
    token_type: Literal["local_llm", "atlassian", "gitlab", "github"]
    value:      str
    label:      Optional[str] = None


class TokenOut(BaseModel):
    token_type: str
    label:      Optional[str]
    masked:     str
    is_active:  bool


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def get_profile(current_user: dict = Depends(get_current_user)):
    """Return own profile including ABAC fields."""
    engine, text = _db()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT id, email, name, role, department,
                           gitlab_username, is_security_team,
                           last_ad_sync, account_status,
                           ad_level, ad_title, ad_username, manager_dn,
                           last_login_at, created_at, hashed_password,
                           is_temp_password
                    FROM users WHERE id = :uid
                """),
                {"uid": current_user["sub"]},
            ).fetchone()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    if not row:
        raise HTTPException(404, detail="User not found")

    import os as _os
    from passlib.context import CryptContext as _CryptCtx
    from core.config import APPROVAL_AD_LEVEL as _APPROVAL_LEVEL
    _hashed_password    = row[15]
    _is_temp_password   = bool(row[16]) if len(row) > 16 else False
    _has_local_password = bool(_hashed_password)
    # Detect whether the user is still on the seeded password from .env.
    # Only checked when a local password exists — LDAP/SSO users are excluded,
    # and only when SEED_ADMIN_PASSWORD is actually configured. There is no
    # hardcoded fallback to compare against: an auto-generated first-boot
    # password is flagged through is_temp_password instead, so the Profile page
    # still nudges the user to change it.
    _SEEDED_PASS = (_os.getenv("SEED_ADMIN_PASSWORD") or "").strip()
    _using_default = False
    if _has_local_password and _SEEDED_PASS:
        try:
            _pwd_ctx = _CryptCtx(schemes=["bcrypt"], deprecated="auto")
            _using_default = _pwd_ctx.verify(_SEEDED_PASS, _hashed_password)
        except Exception:
            _using_default = False

    ad_level = row[9] if row[9] is not None else 6
    return {
        "id":                    str(row[0]),
        "email":                 encrypt_pii(row[1]),
        "name":                  encrypt_pii(row[2]),
        "role":                  row[3],
        "department":            row[4] or "",
        "gitlab_username":       row[5],
        "is_security_team":      row[6] or False,
        "last_ad_sync":          row[7].isoformat() if row[7] else None,
        "account_status":        row[8] or "active",
        "ad_level":              ad_level,
        "ad_title":              row[10] or "",
        "ad_username":           row[11] or "",
        "manager_dn":            row[12] or "",
        "can_approve":           ad_level <= _APPROVAL_LEVEL or row[3] == "admin",
        "last_login_at":         row[13].isoformat() if row[13] else None,
        "member_since":          row[14].isoformat() if row[14] else None,
        # Credential-related flags — used by Profile UI to show/hide
        # the Change Password section and the default-credential warning.
        # has_local_password=false for LDAP/SSO users (no hashed_password in DB).
        "has_local_password":     _has_local_password,
        "using_default_password": _using_default,
        # GAP-6: true when a temporary password was issued via forgot-password flow.
        # Profile UI shows amber banner prompting user to change it immediately.
        "is_temp_password":       _is_temp_password,
    }


@router.put("")
async def update_profile(
    body: ProfileUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update mutable profile fields (name, gitlab_username)."""
    # body.name may be a "pii:v1:" ciphertext sent by the browser (see
    # ai-ui/src/utils/piiCrypto.js) — decrypt before validation/storage
    # (no-op if PII_PAYLOAD_ENCRYPTION_ENABLED is off).
    if body.name is not None:
        try:
            body.name = decrypt_pii(body.name)
        except Exception:
            # Undecryptable ciphertext (rotated/mismatched key). Reject rather
            # than fall through: the raw token would otherwise be validated and
            # written into users.name, replacing the real name with ciphertext.
            raise HTTPException(
                400,
                detail="name could not be decrypted — check that the browser and "
                       "server PII encryption keys match.",
            )
        # Belt-and-braces: a double-encrypted payload or a future format change
        # must never be persisted as if it were the user's display name.
        if looks_encrypted(body.name):
            raise HTTPException(
                400,
                detail="name is still encrypted after decryption — refusing to store ciphertext.",
            )
    # Validate name and gitlab_username for XSS/injection/special chars
    is_valid, field_errors, sanitized = validate_profile_update_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(400, detail="; ".join(error_messages))

    engine, text = _db()
    updates = {}
    if sanitized["name"] is not None:
        updates["name"] = sanitized["name"]
    if sanitized["gitlab_username"] is not None:
        updates["gitlab_username"] = sanitized["gitlab_username"]

    if not updates:
        return {"updated": False}

    set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
    updates["email"] = current_user["email"]

    try:
        with engine.connect() as conn:
            conn.execute(
                text(f"UPDATE users SET {set_clauses} WHERE email = :email"),
                updates,
            )
            conn.commit()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    return {"updated": True}

# ── Custom Instructions (ChatGPT-style persona injection) ────────────────────

@router.get("/custom-instructions")
async def get_custom_instructions(current_user: dict = Depends(get_current_user)):
    """Return the caller's saved Custom Instructions blobs."""
    engine, text = _db()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT custom_about_user, custom_response_style FROM users WHERE id = :uid"),
                {"uid": current_user["sub"]},
            ).fetchone()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    if not row:
        return {"about_user": "", "response_style": ""}
    return {
        "about_user":     row[0] or "",
        "response_style": row[1] or "",
    }


class _CustomInstructionsIn(BaseModel):
    about_user:     Optional[str] = None
    response_style: Optional[str] = None


@router.put("/custom-instructions")
async def set_custom_instructions(
        body: _CustomInstructionsIn,
        current_user: dict = Depends(get_current_user),
):
    """Persist the caller's Custom Instructions. Either field can be None
    to keep the existing value; pass "" to clear."""
    engine, text = _db()
    sets, params = [], {"uid": current_user["sub"]}
    if body.about_user is not None:
        sets.append("custom_about_user = :about")
        params["about"] = (body.about_user or "")[:4000] or None
    if body.response_style is not None:
        sets.append("custom_response_style = :style")
        params["style"] = (body.response_style or "")[:4000] or None
    if not sets:
        return {"updated": False}
    try:
        with engine.connect() as conn:
            conn.execute(
                text(f"UPDATE users SET {', '.join(sets)} WHERE id = :uid"),
                params,
            )
            conn.commit()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    return {"updated": True}


@router.get("/tokens/{token_type}/value")
async def reveal_token(
        token_type: str,
        current_user: dict = Depends(get_current_user),
):
    """Return the caller's OWN decrypted token in plaintext.

    Used by the desktop app to `git clone` on the user's machine with their
    stored GitLab PAT. A user can ONLY ever read their own token (scoped by
    JWT sub) — never anyone else's. Every reveal is audit-logged.
    """
    if token_type not in TOKEN_TYPES:
        raise HTTPException(400, detail=f"Invalid token_type. Must be one of: {TOKEN_TYPES}")

    engine, text = _db()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                     SELECT encrypted_value FROM user_tokens
                     WHERE user_id = :uid AND token_type = :ttype AND is_active = TRUE
                     """),
                {"uid": current_user["sub"], "ttype": token_type},
            ).fetchone()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    if not row:
        raise HTTPException(404, detail=f"No active {token_type} token saved in your profile")
    try:
        token = _decrypt(row[0])
    except Exception:
        raise HTTPException(500, detail="Token could not be decrypted")

    # Security-relevant: the raw PAT leaves the server here. Audit every reveal.
    logger.warning(
        f"Profile: token REVEALED type={token_type} user={current_user.get('email')} "
        f"sub={current_user['sub']} (purpose=git-clone)"
    )
    return {"token_type": token_type, "token": token}

@router.get("/tokens")
async def list_tokens(current_user: dict = Depends(get_current_user)):
    """List own API tokens (masked values only)."""
    engine, text = _db()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT token_type, label, encrypted_value, is_active
                    FROM user_tokens WHERE user_id = :uid AND is_active = TRUE
                """),
                {"uid": current_user["sub"]},
            ).fetchall()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    result = []
    for token_type, label, encrypted_value, is_active in rows:
        try:
            plain = _decrypt(encrypted_value)
            masked = _mask(plain)
        except Exception:
            masked = "****"
        result.append(TokenOut(
            token_type=token_type,
            label=label,
            masked=masked,
            is_active=is_active,
        ))
    return result


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def upsert_token(
    body: TokenUpsert,
    current_user: dict = Depends(get_current_user),
):
    """Add or replace an API token (upsert by user_id + token_type)."""
    if body.token_type not in TOKEN_TYPES:
        raise HTTPException(400, detail=f"Invalid token_type. Must be one of: {TOKEN_TYPES}")

    # Validate value (XSS/script only) and label (special chars)
    is_valid, field_errors, sanitized = validate_token_upsert_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(400, detail="; ".join(error_messages))

    plain = _decrypt_transport(sanitized["value"])
    encrypted = _encrypt(plain)
    engine, text = _db()

    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO user_tokens (user_id, token_type, encrypted_value, label, is_active)
                    VALUES (:uid, :ttype, :enc, :label, TRUE)
                    ON CONFLICT (user_id, token_type) DO UPDATE
                        SET encrypted_value = :enc,
                            label           = :label,
                            is_active       = TRUE,
                            updated_at      = NOW()
                """),
                {
                    "uid":   current_user["sub"],
                    "ttype": body.token_type,
                    "enc":   encrypted,
                    "label": sanitized["label"],
                },
            )
            conn.commit()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    logger.info(f"Profile: token upserted type={body.token_type} user={mask_email(current_user['email'])}")
    return {"saved": True, "token_type": body.token_type}


@router.delete("/tokens/{token_type}", status_code=status.HTTP_200_OK)
async def delete_token(
    token_type: str,
    current_user: dict = Depends(get_current_user),
):
    """Deactivate (soft-delete) an API token."""
    if token_type not in TOKEN_TYPES:
        raise HTTPException(400, detail=f"Invalid token_type")

    engine, text = _db()
    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                    DELETE FROM user_tokens
                    WHERE user_id = :uid AND token_type = :ttype
                """),
                {"uid": current_user["sub"], "ttype": token_type},
            )
            conn.commit()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    return {"deleted": True, "token_type": token_type}


def get_decrypted_token(user_id: str, token_type: str) -> Optional[str]:
    """
    Internal helper — called by model_router for the in-house model tier.
    Returns decrypted token value or None if not found / inactive.
    """
    from db.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT encrypted_value FROM user_tokens
                    WHERE user_id = :uid AND token_type = :ttype AND is_active = TRUE
                """),
                {"uid": user_id, "ttype": token_type},
            ).fetchone()
        if row:
            return _decrypt(row[0])
    except Exception as exc:
        logger.warning(f"get_decrypted_token failed for {user_id}/{token_type}: {exc}")
    return None
