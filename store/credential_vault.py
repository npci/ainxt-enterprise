# SPDX-License-Identifier: MIT
# ============================================================
# CREDENTIAL VAULT STORE
# AES-256-GCM authenticated encryption (cryptography.hazmat AESGCM)
#
# SEC-F-020 / SEC-F-032 (2026-08-26): migrated off Fernet (AES-128-CBC +
# HMAC-SHA256) to AES-256-GCM. Fernet was cryptographically sound for this
# use case — no known practical attack — so this is a policy-driven upgrade
# to a 256-bit key + AEAD construction, not a response to a vulnerability.
#
# Ciphertext format (current): "v2:" + base64url(12-byte nonce || GCM
# ciphertext+tag). decrypt_value() also accepts a legacy Fernet token (no
# "v2:" prefix) so rows written before this migration keep decrypting — no
# forced bulk migration required. Any value re-written via
# update_credential()/rotate_credential() is re-encrypted under AES-256-GCM
# automatically. To fully retire Fernet reads, re-save every row once
# (update_credential(name, value=<same decrypted value>)).
#
# Env var: FERNET_KEY (preferred) or VAULT_ENCRYPTION_KEY (legacy alias) —
#   URL-safe base64-encoded 32-byte key. Generate with:
#     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   The same key material is reused as the raw AES-256-GCM key (its 32
#   decoded bytes) — no new secret needs to be provisioned for this migration.
#   Must be set; there is no ephemeral-key fallback (see SEC-F-012).
#
# Storage: SQLAlchemy SessionLocal → CredentialVault ORM model.
# Graceful fallback: if Postgres is unavailable the store transparently
# falls back to an in-process dict (data is lost on restart; WARNING logged).
# ============================================================

import base64
import os
import uuid
from datetime import datetime
from typing import Optional, List, Dict

from core.logger import logger

_V2_PREFIX = "v2:"
_NONCE_LEN = 12  # 96-bit GCM nonce (NIST SP 800-38D recommended size)


# ── Key bootstrap ─────────────────────────────────────────────

_aesgcm_instance = None
_fernet_instance = None  # legacy — decrypt-only, for rows written before this migration


def _raw_key_bytes() -> bytes:
    """Resolve the configured 32-byte key material.
    Key resolution (first non-empty wins):
      1. FERNET_KEY            — the platform-standard vault key (also used by
                                 core/platform_credentials.py + the user_tokens
                                 GitLab/Jira encryption); reused here so ALL
                                 vaulted secrets share one stable key and no new
                                 secret has to be provisioned for this migration.
      2. VAULT_ENCRYPTION_KEY  — legacy alias, kept for backward compatibility.
    Raises RuntimeError if neither is set — no ephemeral-key fallback
    (SEC-F-012: a silently-generated key makes every stored secret
    unreadable after a restart).
    """
    raw_key = (os.getenv("FERNET_KEY", "") or os.getenv("VAULT_ENCRYPTION_KEY", "")).strip()
    if not raw_key:
        raise RuntimeError("FERNET_KEY (or VAULT_ENCRYPTION_KEY) must be set.")
    try:
        # Fernet keys are always 32 bytes once base64-decoded — reuse those
        # same 32 bytes as the AES-256-GCM key so no new secret is needed.
        key = base64.urlsafe_b64decode(raw_key.encode())
    except Exception:
        raise ValueError("FERNET_KEY is invalid — check format (must be URL-safe base64, 32 bytes).")
    if len(key) != 32:
        raise ValueError("FERNET_KEY is invalid — must decode to exactly 32 bytes for AES-256-GCM.")
    return key


def _get_aesgcm():
    global _aesgcm_instance
    if _aesgcm_instance is None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        _aesgcm_instance = AESGCM(_raw_key_bytes())
        logger.info("CredentialVault: AES-256-GCM key loaded from FERNET_KEY/VAULT_ENCRYPTION_KEY")
    return _aesgcm_instance


def _get_fernet():
    """Legacy Fernet instance — decrypt-only, for ciphertexts written before
    the AES-256-GCM migration. Uses the SAME key material as _get_aesgcm()."""
    global _fernet_instance
    if _fernet_instance is None:
        from cryptography.fernet import Fernet
        raw_key = (os.getenv("FERNET_KEY", "") or os.getenv("VAULT_ENCRYPTION_KEY", "")).strip()
        if not raw_key:
            raise RuntimeError("FERNET_KEY (or VAULT_ENCRYPTION_KEY) must be set.")
        try:
            _fernet_instance = Fernet(raw_key.encode())
        except Exception:
            raise ValueError("FERNET_KEY is invalid — check format (must be URL-safe base64, 32 bytes).")
    return _fernet_instance


# ── Encryption helpers ───────────────────────────────────────

def encrypt_value(plaintext: str) -> str:
    """Encrypt *plaintext* with AES-256-GCM.
    Returns "v2:" + base64url(nonce || ciphertext+tag) as a UTF-8 string."""
    import secrets as _secrets
    nonce = _secrets.token_bytes(_NONCE_LEN)
    ciphertext = _get_aesgcm().encrypt(nonce, plaintext.encode("utf-8"), None)
    token = base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")
    return _V2_PREFIX + token


def decrypt_value(token: str) -> str:
    """Decrypt a value back to plaintext.

    Accepts both the current AES-256-GCM format ("v2:" prefix) and legacy
    Fernet tokens (no prefix) written before the SEC-F-020/032 migration, so
    existing rows keep decrypting without a forced bulk re-encryption pass.
    Raises on tampered ciphertext or wrong key (InvalidTag / InvalidToken)."""
    if token.startswith(_V2_PREFIX):
        raw = base64.urlsafe_b64decode(token[len(_V2_PREFIX):].encode("utf-8"))
        nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        plaintext_bytes = _get_aesgcm().decrypt(nonce, ciphertext, None)
        return plaintext_bytes.decode("utf-8")

    # Legacy Fernet token (pre-migration row) — decrypt-only.
    plaintext_bytes = _get_fernet().decrypt(token.encode("utf-8"))
    return plaintext_bytes.decode("utf-8")


# ── In-process fallback store ────────────────────────────────
# Keyed by credential name. Used transparently when Postgres is unavailable.

_fallback_store: Dict[str, dict] = {}


# ── Internal serialisation helpers ──────────────────────────

def _row_to_dict(row) -> dict:
    """Convert a CredentialVault ORM row to a safe public dict (no encrypted field)."""
    return {
        "id":           str(row.id),
        "name":         row.name,
        "description":  row.description,
        "category":     row.category,
        "tags":         row.tags or [],
        "owner_id":     str(row.owner_id) if row.owner_id else None,
        "last_rotated": row.last_rotated.isoformat() if row.last_rotated else None,
        "created_at":   row.created_at.isoformat() if row.created_at else None,
        "updated_at":   row.updated_at.isoformat() if row.updated_at else None,
    }


def _fallback_public(entry: dict) -> dict:
    """Strip the encrypted field from a fallback-store entry for safe external return."""
    return {k: v for k, v in entry.items() if k != "encrypted"}


# ── CRUD operations ──────────────────────────────────────────

def create_credential(
    name: str,
    value: str,
    description: Optional[str] = None,
    category: str = "api_key",
    tags: Optional[List[str]] = None,
    owner_id: Optional[str] = None,
) -> dict:
    """Encrypt *value* and persist a new credential record.

    Returns a public dict (id, name, description, category, tags, owner_id,
    last_rotated, created_at, updated_at) — never the encrypted field.
    Raises ValueError if a credential with *name* already exists.
    """
    tags = tags or []

    # ── DB path ──────────────────────────────────────────────
    try:
        from db.database import SessionLocal
        from db.models import CredentialVault

        encrypted = encrypt_value(value)
        db = SessionLocal()
        try:
            existing = db.query(CredentialVault).filter(CredentialVault.name == name).first()
            if existing:
                raise ValueError(f"Credential '{name}' already exists")

            record = CredentialVault(
                id=str(uuid.uuid4()),
                name=name,
                description=description,
                category=category,
                encrypted=encrypted,
                owner_id=owner_id,
                tags=tags,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            result = _row_to_dict(record)
            logger.info(f"CredentialVault: created credential '{name}' (category={category})")
            return result
        finally:
            db.close()

    except ValueError:
        raise
    except Exception as exc:
        logger.warning(f"CredentialVault: DB unavailable, using fallback store — {exc}")

    # ── Fallback path ─────────────────────────────────────────
    if name in _fallback_store:
        raise ValueError(f"Credential '{name}' already exists")

    now = datetime.utcnow().isoformat()
    entry = {
        "id":           str(uuid.uuid4()),
        "name":         name,
        "description":  description,
        "category":     category,
        "encrypted":    encrypt_value(value),
        "tags":         tags,
        "owner_id":     owner_id,
        "last_rotated": None,
        "created_at":   now,
        "updated_at":   now,
    }
    _fallback_store[name] = entry
    logger.info(f"CredentialVault[fallback]: created credential '{name}'")
    return _fallback_public(entry)


def get_credential(name: str) -> Optional[dict]:
    """Return credential metadata for *name*.  Does NOT return the decrypted value.
    Use get_credential_value() to retrieve the plaintext.
    Returns None if not found.
    """
    try:
        from db.database import SessionLocal
        from db.models import CredentialVault

        db = SessionLocal()
        try:
            row = db.query(CredentialVault).filter(CredentialVault.name == name).first()
            return _row_to_dict(row) if row else None
        finally:
            db.close()

    except Exception as exc:
        logger.warning(f"CredentialVault: DB unavailable, reading from fallback store — {exc}")

    entry = _fallback_store.get(name)
    return _fallback_public(entry) if entry else None


def get_credential_value(name: str) -> Optional[str]:
    """Return the *decrypted* plaintext value for *name*.
    Returns None if not found.
    Callers MUST emit an audit log entry before calling this function.
    """
    try:
        from db.database import SessionLocal
        from db.models import CredentialVault

        db = SessionLocal()
        try:
            row = db.query(CredentialVault).filter(CredentialVault.name == name).first()
            if not row:
                return None
            return decrypt_value(row.encrypted)
        finally:
            db.close()

    except Exception as exc:
        logger.warning(f"CredentialVault: DB unavailable, reading from fallback store — {exc}")

    entry = _fallback_store.get(name)
    if not entry:
        return None
    return decrypt_value(entry["encrypted"])


def update_credential(
    name: str,
    value: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Optional[dict]:
    """Update an existing credential.  *value*, *description*, and *tags* are
    each optional — only supplied fields are changed.
    Returns the updated public dict or None if the credential is not found.
    """
    try:
        from db.database import SessionLocal
        from db.models import CredentialVault

        db = SessionLocal()
        try:
            row = db.query(CredentialVault).filter(CredentialVault.name == name).first()
            if not row:
                return None
            if value is not None:
                row.encrypted = encrypt_value(value)
            if description is not None:
                row.description = description
            if tags is not None:
                row.tags = tags
            row.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            logger.info(f"CredentialVault: updated credential '{name}'")
            return _row_to_dict(row)
        finally:
            db.close()

    except Exception as exc:
        logger.warning(f"CredentialVault: DB unavailable, updating fallback store — {exc}")

    entry = _fallback_store.get(name)
    if not entry:
        return None
    if value is not None:
        entry["encrypted"] = encrypt_value(value)
    if description is not None:
        entry["description"] = description
    if tags is not None:
        entry["tags"] = tags
    entry["updated_at"] = datetime.utcnow().isoformat()
    logger.info(f"CredentialVault[fallback]: updated credential '{name}'")
    return _fallback_public(entry)


def delete_credential(name: str) -> bool:
    """Delete credential *name*.  Returns True if deleted, False if not found."""
    try:
        from db.database import SessionLocal
        from db.models import CredentialVault

        db = SessionLocal()
        try:
            row = db.query(CredentialVault).filter(CredentialVault.name == name).first()
            if not row:
                return False
            db.delete(row)
            db.commit()
            logger.info(f"CredentialVault: deleted credential '{name}'")
            return True
        finally:
            db.close()

    except Exception as exc:
        logger.warning(f"CredentialVault: DB unavailable, deleting from fallback store — {exc}")

    if name not in _fallback_store:
        return False
    del _fallback_store[name]
    logger.info(f"CredentialVault[fallback]: deleted credential '{name}'")
    return True


def list_credentials(category: Optional[str] = None) -> List[dict]:
    """Return all credential metadata records.  Never includes the encrypted value.
    Optionally filter by *category* (api_key | oauth_token | password | certificate).
    """
    try:
        from db.database import SessionLocal
        from db.models import CredentialVault

        db = SessionLocal()
        try:
            query = db.query(CredentialVault)
            if category:
                query = query.filter(CredentialVault.category == category)
            rows = query.order_by(CredentialVault.created_at.desc()).all()
            return [_row_to_dict(r) for r in rows]
        finally:
            db.close()

    except Exception as exc:
        logger.warning(f"CredentialVault: DB unavailable, listing from fallback store — {exc}")

    entries = list(_fallback_store.values())
    if category:
        entries = [e for e in entries if e.get("category") == category]
    # Sort newest first (created_at is an ISO string in the fallback)
    entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return [_fallback_public(e) for e in entries]


def rotate_credential(name: str, new_value: str) -> Optional[dict]:
    """Replace the stored encrypted value with a freshly encrypted *new_value*
    and stamp *last_rotated*.
    Returns the updated public dict or None if the credential is not found.
    """
    try:
        from db.database import SessionLocal
        from db.models import CredentialVault

        db = SessionLocal()
        try:
            row = db.query(CredentialVault).filter(CredentialVault.name == name).first()
            if not row:
                return None
            now = datetime.utcnow()
            row.encrypted    = encrypt_value(new_value)
            row.last_rotated = now
            row.updated_at   = now
            db.commit()
            db.refresh(row)
            logger.info(f"CredentialVault: rotated credential '{name}'")
            return _row_to_dict(row)
        finally:
            db.close()

    except Exception as exc:
        logger.warning(f"CredentialVault: DB unavailable, rotating in fallback store — {exc}")

    entry = _fallback_store.get(name)
    if not entry:
        return None
    now_iso = datetime.utcnow().isoformat()
    entry["encrypted"]    = encrypt_value(new_value)
    entry["last_rotated"] = now_iso
    entry["updated_at"]   = now_iso
    logger.info(f"CredentialVault[fallback]: rotated credential '{name}'")
    return _fallback_public(entry)
