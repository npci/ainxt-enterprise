# SPDX-License-Identifier: Apache-2.0
# ============================================================
# core.pii_crypto — env-flag-gated AES-256-GCM encryption for
# sensitive fields (email, name, phone/mobile) placed in OUTGOING
# API response payloads.
#
# Scope: payload-only. This module never touches how these fields
# are stored in the database — only the value written into a JSON
# response, and (symmetrically) the value read back out of a payload
# before it is passed as a parameter to a method that needs the
# real, plaintext value.
#
# Gating: PII_PAYLOAD_ENCRYPTION_ENABLED (default "false"). When
# disabled, encrypt_pii()/decrypt_pii() are no-ops — callers can
# wrap every sensitive field unconditionally and behavior is
# byte-for-byte identical to not calling this module at all.
#
# Ciphertext format: "pii:v1:" + base64url(12-byte nonce || AES-256-GCM
# ciphertext+tag) — same AESGCM construction already used by
# store/credential_vault.py, just a distinct prefix/key so PII payload
# encryption is independently rotatable from the credential vault key.
#
# Env var: PII_ENCRYPTION_KEY — URL-safe base64-encoded 32-byte key
#   (same format as FERNET_KEY). Generate with:
#     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   Required only when PII_PAYLOAD_ENCRYPTION_ENABLED=true. No
#   ephemeral-key fallback — fails closed with a clear error rather
#   than silently generating a key that makes ciphertext unreadable
#   after a restart.
# ============================================================

from __future__ import annotations

import base64
import os
import secrets
from typing import Optional

from core.logger import logger

_PREFIX = "pii:v1:"
_NONCE_LEN = 12  # 96-bit GCM nonce (NIST SP 800-38D recommended size)

_aesgcm_instance = None


def pii_encryption_enabled() -> bool:
    """Read the master flag fresh on every call (env can differ per test/process)."""
    return os.getenv("PII_PAYLOAD_ENCRYPTION_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _raw_key_bytes() -> bytes:
    raw_key = os.getenv("PII_ENCRYPTION_KEY", "").strip()
    if not raw_key:
        raise RuntimeError(
            "PII_ENCRYPTION_KEY must be set when PII_PAYLOAD_ENCRYPTION_ENABLED=true."
        )
    try:
        key = base64.urlsafe_b64decode(raw_key.encode())
    except Exception:
        raise ValueError(
            "PII_ENCRYPTION_KEY is invalid — check format (must be URL-safe base64, 32 bytes)."
        )
    if len(key) != 32:
        raise ValueError(
            "PII_ENCRYPTION_KEY is invalid — must decode to exactly 32 bytes for AES-256-GCM."
        )
    return key


def _get_aesgcm():
    global _aesgcm_instance
    if _aesgcm_instance is None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        _aesgcm_instance = AESGCM(_raw_key_bytes())
        logger.info("pii_crypto: AES-256-GCM key loaded from PII_ENCRYPTION_KEY")
    return _aesgcm_instance


def encrypt_pii(value: Optional[str]) -> Optional[str]:
    """Encrypt *value* for placement into an outgoing payload.

    No-op (returns *value* unchanged) when the flag is disabled or the
    value is falsy (None/""). Never raises when disabled.
    """
    if not value or not pii_encryption_enabled():
        return value
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = _get_aesgcm().encrypt(nonce, value.encode("utf-8"), None)
    token = base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")
    return _PREFIX + token


def decrypt_pii(value: Optional[str]) -> Optional[str]:
    """Decrypt a value previously produced by encrypt_pii().

    Safe no-op (returns *value* unchanged) when the flag is disabled, the
    value is falsy, or the value doesn't carry the pii:v1: prefix (already
    plaintext) — so callers can pass any string through unconditionally.
    """
    if not value or not pii_encryption_enabled():
        return value

    # Unwrap repeatedly rather than once.
    #
    # A browser that could not decrypt an incoming field (no/rotated
    # VITE_PII_ENCRYPTION_KEY) renders the raw "pii:v1:..." token into an
    # editable input. On submit it encrypts that token *again*, so what
    # arrives here is encrypt(encrypt(plaintext)). A single unwrap would
    # yield a still-encrypted string and persist it to the database as if it
    # were the user's real name — silent, permanent PII corruption. Looping
    # until the prefix is gone makes the operation idempotent in the number
    # of encryption layers. The bound keeps a malicious/corrupt payload from
    # spinning the CPU.
    current = value
    for _ in range(4):
        if not current.startswith(_PREFIX):
            return current
        token = current[len(_PREFIX):]
        # Re-add base64 padding before decoding. The browser counterpart
        # (ai-ui/src/utils/piiCrypto.js) strips trailing "=" to produce
        # canonical base64url, so a token minted in the browser is 1-2 chars
        # short of a multiple of 4 and base64.urlsafe_b64decode() would raise
        # "Incorrect padding". Tokens minted here are already padded, in
        # which case this is a no-op — both directions share one code path.
        token += "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token.encode("utf-8"))
        nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        current = _get_aesgcm().decrypt(nonce, ciphertext, None).decode("utf-8")
    return current


def looks_encrypted(value: Optional[str]) -> bool:
    """True when *value* still carries the pii:v1: prefix.

    Lets a write path assert that a decrypt actually produced plaintext
    before persisting it, so ciphertext is never stored in a PII column.
    """
    return bool(value) and value.startswith(_PREFIX)
