# SPDX-License-Identifier: Apache-2.0
# ============================================================
# tests/core/ckms/test_crypto.py — AES-GCM decryption round-trip
# ============================================================

from __future__ import annotations

import base64
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.ckms.crypto import (
    CipherAuthError,
    CipherFormatError,
    aes_gcm_decrypt,
)


def _encrypt(plaintext: str, key: bytes) -> str:
    """Test helper: produce the wire format the requirement specifies."""
    iv = os.urandom(12)
    ct_and_tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return (
        base64.b64encode(iv).decode("ascii")
        + ":"
        + base64.b64encode(ct_and_tag).decode("ascii")
    )


def test_round_trip_aes_256():
    key = os.urandom(32)
    ct = _encrypt("hello world", key)
    assert aes_gcm_decrypt(ct, key) == "hello world"


def test_round_trip_unicode():
    key = os.urandom(32)
    ct = _encrypt("emoji-✨-and-कुछ-non-ascii", key)
    assert aes_gcm_decrypt(ct, key) == "emoji-✨-and-कुछ-non-ascii"


def test_missing_separator_raises():
    key = os.urandom(32)
    with pytest.raises(CipherFormatError):
        aes_gcm_decrypt("no-separator-here", key)


def test_bad_base64_raises():
    key = os.urandom(32)
    with pytest.raises(CipherFormatError):
        aes_gcm_decrypt("!!!:!!!", key)


def test_short_iv_raises():
    key = os.urandom(32)
    # 8-byte IV instead of 12
    short_iv = base64.b64encode(os.urandom(8)).decode("ascii")
    body = base64.b64encode(os.urandom(32)).decode("ascii")
    with pytest.raises(CipherFormatError):
        aes_gcm_decrypt(f"{short_iv}:{body}", key)


def test_wrong_key_raises_auth_error():
    real_key = os.urandom(32)
    wrong_key = os.urandom(32)
    ct = _encrypt("secret", real_key)
    with pytest.raises(CipherAuthError):
        aes_gcm_decrypt(ct, wrong_key)


def test_non_bytes_key_raises():
    ct = _encrypt("x", os.urandom(32))
    with pytest.raises(CipherFormatError):
        aes_gcm_decrypt(ct, "not-bytes")  # type: ignore[arg-type]
