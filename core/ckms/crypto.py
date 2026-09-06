# SPDX-License-Identifier: MIT
# ============================================================
# core.ckms.crypto — AES-256-GCM decryption
#
# Ciphertext wire format (per requirement §"Ciphertext Format"):
#
#     <base64(iv)>:<base64(ciphertext||tag)>
#
# - AES-256-GCM
# - 12-byte IV, generated per-encryption
# - Authentication tag appended to ciphertext (standard cryptography layout)
# - ':' is the separator
# - The clear DEK (32 bytes) from keys_table is the AES key
#
# Only DECRYPTION is implemented here. Encryption is handled by ops tooling
# and is explicitly out of scope (requirement §"Out of Scope").
# ============================================================

from __future__ import annotations

import base64
import binascii

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CipherFormatError(ValueError):
    """Malformed ciphertext (missing separator, bad base64, wrong IV size, etc.)."""


class CipherAuthError(ValueError):
    """AES-GCM authentication tag did not verify — wrong key or tampering."""


_SEPARATOR = ":"
_IV_LEN_BYTES = 12


def aes_gcm_decrypt(ciphertext: str, key: bytes) -> str:
    """
    Decrypt a CKMS-protected env value.

    Args:
        ciphertext: ``<b64(iv)>:<b64(ct||tag)>`` string.
        key: 16, 24, or 32-byte clear DEK. CKMS uses 32 (AES-256).

    Returns:
        Plaintext as a UTF-8 ``str``.

    Raises:
        CipherFormatError: malformed input (no ':', invalid base64, wrong IV size).
        CipherAuthError: GCM tag verification failed.
    """
    if not isinstance(ciphertext, str) or _SEPARATOR not in ciphertext:
        raise CipherFormatError(
            f"ciphertext missing '{_SEPARATOR}' separator"
        )

    iv_b64, ct_b64 = ciphertext.split(_SEPARATOR, 1)
    try:
        iv = base64.b64decode(iv_b64, validate=True)
        ct_and_tag = base64.b64decode(ct_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CipherFormatError(f"invalid base64 in ciphertext: {exc}") from exc

    if len(iv) != _IV_LEN_BYTES:
        raise CipherFormatError(
            f"IV must be {_IV_LEN_BYTES} bytes, got {len(iv)}"
        )

    if not isinstance(key, (bytes, bytearray)):
        raise CipherFormatError("key must be bytes")

    aead = AESGCM(bytes(key))
    try:
        plaintext_bytes = aead.decrypt(iv, ct_and_tag, associated_data=None)
    except InvalidTag as exc:
        # Never include key material or ciphertext in the error message.
        raise CipherAuthError("AES-GCM tag verification failed") from exc

    return plaintext_bytes.decode("utf-8")
