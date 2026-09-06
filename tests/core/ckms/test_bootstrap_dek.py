# SPDX-License-Identifier: MIT
# ============================================================
# tests/core/ckms/test_bootstrap_dek.py
#
# Covers resolve_bootstrap_dek() — the env-var-sourced DEK that
# decrypts DB-connectivity vars BEFORE keys_table is read.
# ============================================================

from __future__ import annotations

import base64
import os

import pytest

from core.ckms.bootstrap_dek import (
    ENV_BOOTSTRAP_DEK,
    ENV_BOOTSTRAP_KEK,
    resolve_bootstrap_dek,
)
from core.ckms.key_service import KeyServiceError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV_BOOTSTRAP_DEK, raising=False)
    monkeypatch.delenv(ENV_BOOTSTRAP_KEK, raising=False)
    yield


def test_returns_none_when_unset():
    assert resolve_bootstrap_dek() is None


def test_returns_none_when_empty_string(monkeypatch):
    monkeypatch.setenv(ENV_BOOTSTRAP_DEK, "   ")
    assert resolve_bootstrap_dek() is None


def test_base_form_decoded_to_32_bytes(monkeypatch):
    raw = os.urandom(32)
    monkeypatch.setenv(
        ENV_BOOTSTRAP_DEK,
        "BASE:" + base64.b64encode(raw).decode("ascii"),
    )
    assert resolve_bootstrap_dek() == raw


def test_base_form_with_invalid_b64_raises(monkeypatch):
    monkeypatch.setenv(ENV_BOOTSTRAP_DEK, "BASE:!!!not-base64!!!")
    with pytest.raises(KeyServiceError) as exc_info:
        resolve_bootstrap_dek()
    assert "BASE:" in str(exc_info.value)


def test_hsm_form_without_kek_raises(monkeypatch):
    monkeypatch.setenv(ENV_BOOTSTRAP_DEK, "AABBCC112233")
    # CKMS_BOOTSTRAP_KEK deliberately unset
    with pytest.raises(KeyServiceError) as exc_info:
        resolve_bootstrap_dek()
    assert ENV_BOOTSTRAP_KEK in str(exc_info.value)


def test_hsm_form_invokes_gateway(monkeypatch):
    """Non-BASE form + KEK present → HSMGateway.unwrap_dek is called."""
    expected = os.urandom(32)
    calls = []

    class _FakeGateway:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def unwrap_dek(self, dek, kek):
            calls.append((dek, kek))
            return expected

    import core.ckms.bootstrap_dek as mod
    import core.ckms.hsm_gateway as hsm_mod

    # The import is local inside resolve_bootstrap_dek; patch the source.
    monkeypatch.setattr(hsm_mod, "HSMGateway", _FakeGateway)
    monkeypatch.setattr(mod, "HSMGateway", _FakeGateway, raising=False)

    monkeypatch.setenv(ENV_BOOTSTRAP_DEK, "AABBCC112233")
    monkeypatch.setenv(ENV_BOOTSTRAP_KEK, "DDEEFF445566")

    out = resolve_bootstrap_dek()

    assert out == expected
    assert calls == [("AABBCC112233", "DDEEFF445566")]
