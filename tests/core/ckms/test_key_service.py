# SPDX-License-Identifier: Apache-2.0
# ============================================================
# tests/core/ckms/test_key_service.py — KeyService singleton behaviour
# ============================================================

from __future__ import annotations

import base64
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.ckms.key_service import KeyService, KeyServiceError


def _encrypt(plaintext: str, key: bytes) -> str:
    iv = os.urandom(12)
    ct_and_tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return (
        base64.b64encode(iv).decode("ascii")
        + ":"
        + base64.b64encode(ct_and_tag).decode("ascii")
    )


@pytest.fixture(autouse=True)
def _reset_singleton():
    KeyService.reset_for_tests()
    yield
    KeyService.reset_for_tests()


def test_install_then_decrypt_uses_default_key_type():
    key = os.urandom(32)
    svc = KeyService.instance()
    svc.install(cache={"KEY_CREDS": key}, mapping={})
    ct = _encrypt("hello", key)
    assert svc.decrypt("ANY_NEW_VAR", ct) == "hello"


def test_mapping_overrides_default():
    k_creds = os.urandom(32)
    t_creds = os.urandom(32)
    svc = KeyService.instance()
    svc.install(
        cache={"KEY_CREDS": k_creds, "TOKEN_CREDS": t_creds},
        mapping={"GITLAB_TOKEN": "TOKEN_CREDS"},
    )
    ct = _encrypt("token-xyz", t_creds)
    # Decrypt fails if we tried to use KEY_CREDS — proves mapping was honoured.
    assert svc.decrypt("GITLAB_TOKEN", ct) == "token-xyz"


def test_install_is_idempotent():
    k1 = os.urandom(32)
    k2 = os.urandom(32)
    svc = KeyService.instance()
    svc.install(cache={"KEY_CREDS": k1}, mapping={})
    svc.install(cache={"KEY_CREDS": k2}, mapping={})  # second call is a no-op
    assert svc.clear_dek("KEY_CREDS") == k1


def test_missing_key_type_raises():
    svc = KeyService.instance()
    svc.install(cache={"KEY_CREDS": os.urandom(32)}, mapping={})
    with pytest.raises(KeyServiceError):
        svc.clear_dek("UNKNOWN")


def test_decrypt_env_reads_from_os_environ(monkeypatch):
    key = os.urandom(32)
    svc = KeyService.instance()
    svc.install(cache={"KEY_CREDS": key}, mapping={})
    decrypted_fixture_value = os.environ.get("TEST_DECRYPT_VAL", "test-decrypted-value")
    ct = _encrypt(decrypted_fixture_value, key)
    monkeypatch.setenv("MY_SECRET", ct)
    assert svc.decrypt_env("MY_SECRET") == decrypted_fixture_value


def test_decrypt_env_missing_raises(monkeypatch):
    svc = KeyService.instance()
    svc.install(cache={"KEY_CREDS": os.urandom(32)}, mapping={})
    monkeypatch.delenv("DOES_NOT_EXIST_X", raising=False)
    with pytest.raises(KeyServiceError):
        svc.decrypt_env("DOES_NOT_EXIST_X")


def test_singleton_returns_same_instance():
    a = KeyService.instance()
    b = KeyService.instance()
    assert a is b
