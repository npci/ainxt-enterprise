# SPDX-License-Identifier: MIT
# ============================================================
# COACH INGESTOR — at-rest crypto for prompt_redacted
# ============================================================
#
# The Coach pipeline is redact-at-write: by the time text reaches the
# ingestor it has already passed through compliance_engine.redact_text(),
# so no raw PAN/PII/secret survives. As an additional defence-in-depth
# layer we encrypt the (already redacted) prompt at rest with AES-256-GCM,
# keyed by COACH_FERNET_KEY (falls back to FERNET_KEY).
#
# SEC-F-020/032 follow-up (2026-08-26): migrated from Fernet (AES-128-CBC +
# HMAC) to AES-256-GCM, the same direction as store/credential_vault.py and
# routers/profile_router.py's user_tokens store. This module keeps its own
# key resolution (COACH_FERNET_KEY, a deliberately separate namespace from
# the platform vault key) rather than delegating to credential_vault's
# functions, so it implements AES-256-GCM directly here — reusing the exact
# same nonce/prefix scheme.
#
# Ciphertext format:
#   "enc:v2:" + base64url(12-byte nonce || AES-256-GCM ciphertext+tag)  — current
#   "enc:v1:" + Fernet token                                            — legacy
#   (no prefix)                                                        — dev-mode plaintext
#
# Behaviour:
#   - prod (key present)  → encrypt on write (always AES-256-GCM), decrypt on
#                            read (auto-detects v2/v1/plaintext by prefix).
#   - dev  (no key)       → store redacted plaintext, return as-is.
#
# Every function is fail-safe: an encryption error never loses the event —
# it degrades to storing the redacted plaintext.
# ============================================================

from __future__ import annotations

import base64

from core.logger import logger

try:
    from core.config import COACH_FERNET_KEY
except Exception:  # pragma: no cover — config import guard
    import os
    COACH_FERNET_KEY = os.getenv("COACH_FERNET_KEY", os.getenv("FERNET_KEY", ""))

# Prefix markers so decrypt() can tell which scheme produced a given value.
_ENC_PREFIX_V1 = "enc:v1:"   # legacy — Fernet (AES-128-CBC + HMAC)
_ENC_PREFIX_V2 = "enc:v2:"   # current — AES-256-GCM
_NONCE_LEN = 12              # 96-bit GCM nonce (NIST SP 800-38D recommended size)

_fernet = None
_fernet_init = False
_aesgcm = None
_aesgcm_init = False


def _key_bytes():
    """Resolve COACH_FERNET_KEY (or FERNET_KEY fallback) to 32 raw bytes for
    AES-256-GCM, or None if unset/invalid. Fernet keys are always 32 bytes
    once base64-decoded, so the SAME configured key doubles as the raw
    AES-256-GCM key — no new secret needed for this migration."""
    key = (COACH_FERNET_KEY or "").strip()
    if not key:
        return None
    try:
        return base64.urlsafe_b64decode(key.encode())
    except Exception as e:
        logger.warning(f"coach.crypto: invalid COACH_FERNET_KEY/FERNET_KEY ({e.__class__.__name__}: {e})")
        return None


def _get_aesgcm():
    """Lazily build the AESGCM instance. Returns None when no key is
    configured (dev mode) or when the cryptography library is unavailable."""
    global _aesgcm, _aesgcm_init
    if _aesgcm_init:
        return _aesgcm
    _aesgcm_init = True

    key = _key_bytes()
    if key is None:
        logger.info("coach.crypto: no COACH_FERNET_KEY/FERNET_KEY — storing redacted plaintext (dev mode)")
        _aesgcm = None
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        _aesgcm = AESGCM(key)
        logger.info("coach.crypto: AES-256-GCM encryption enabled for prompt_redacted")
    except Exception as e:
        logger.warning(f"coach.crypto: AES-256-GCM init failed ({e.__class__.__name__}: {e}) — falling back to plaintext")
        _aesgcm = None
    return _aesgcm


def _get_fernet():
    """Lazily build a Fernet instance — legacy decrypt-only path, for values
    written before the AES-256-GCM migration. Uses the SAME key material as
    _get_aesgcm()."""
    global _fernet, _fernet_init
    if _fernet_init:
        return _fernet
    _fernet_init = True

    key = (COACH_FERNET_KEY or "").strip()
    if not key:
        _fernet = None
        return None
    try:
        from cryptography.fernet import Fernet  # type: ignore
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        logger.warning(f"coach.crypto: legacy Fernet init failed ({e.__class__.__name__}: {e})")
        _fernet = None
    return _fernet


def encrypt(text: str | None) -> str | None:
    """Encrypt redacted text for at-rest storage with AES-256-GCM. Returns
    None unchanged. On any failure (or no key) returns the input plaintext
    so the event is never lost."""
    if text is None:
        return None
    aesgcm = _get_aesgcm()
    if aesgcm is None:
        return text
    try:
        import secrets as _secrets
        nonce = _secrets.token_bytes(_NONCE_LEN)
        ciphertext = aesgcm.encrypt(nonce, text.encode("utf-8"), None)
        token = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return _ENC_PREFIX_V2 + token
    except Exception as e:
        logger.warning(f"coach.crypto: encrypt failed ({e.__class__.__name__}) — storing plaintext")
        return text


def decrypt(stored: str | None) -> str | None:
    """Decrypt a value previously produced by encrypt(). Values without a
    recognised prefix are returned as-is (dev-mode plaintext / very old rows).
    Accepts both the current AES-256-GCM format (enc:v2:) and legacy Fernet
    tokens (enc:v1:) written before the SEC-F-020/032 migration."""
    if stored is None:
        return None

    if stored.startswith(_ENC_PREFIX_V2):
        aesgcm = _get_aesgcm()
        if aesgcm is None:
            # Key was removed after data was encrypted — cannot recover.
            return "[encrypted — key unavailable]"
        try:
            raw = base64.urlsafe_b64decode(stored[len(_ENC_PREFIX_V2):].encode("ascii"))
            nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
            return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
        except Exception as e:
            logger.warning(f"coach.crypto: decrypt (v2) failed ({e.__class__.__name__})")
            return "[decryption failed]"

    if stored.startswith(_ENC_PREFIX_V1):
        f = _get_fernet()
        if f is None:
            return "[encrypted — key unavailable]"
        try:
            token = stored[len(_ENC_PREFIX_V1):]
            return f.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception as e:
            logger.warning(f"coach.crypto: decrypt (v1/legacy) failed ({e.__class__.__name__})")
            return "[decryption failed]"

    return stored  # dev-mode plaintext or pre-versioning legacy row
