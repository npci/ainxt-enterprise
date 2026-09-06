# SPDX-License-Identifier: MIT
# ============================================================
# core/audit_signer.py key validation.
#
# The audit log is tamper-evident only if its signing key is secret.
# `.env.example` ships AUDIT_SIGNING_KEY=change-me-in-production, and the only
# guard used to be "is it non-empty" — in core/audit_signer.py AND in
# validate_prod_config(). So an install that copied the template signed its
# audit log with a value published in this repository: anyone could forge an
# entry and verify_event() would accept it.
#
# These tests pin both halves of the fix: the placeholder is rejected, and a
# legitimate key that merely *contains* a suspicious word is not. The second
# half matters as much as the first — a false positive stops a correctly
# configured deployment from booting at all.
# ============================================================

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "core" / "audit_signer.py"


def _load_signer():
    """Load audit_signer directly from its path.

    It raises at import time when the key is unusable, which is deliberate. A
    plain `import core.audit_signer` would therefore depend on whatever the
    ambient environment happens to hold; loading a fresh module object with a
    known-good key set keeps these tests independent of that.
    """
    os.environ["AUDIT_SIGNING_KEY"] = "a" * 64
    spec = importlib.util.spec_from_file_location("_audit_signer_under_test", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def signer():
    return _load_signer()


# ── Values that must be refused ──────────────────────────────────────────────

@pytest.mark.parametrize("value, why", [
    (None,                        "unset"),
    ("",                          "empty"),
    ("    ",                      "whitespace only"),
    ("change-me-in-production",   "the literal value shipped in .env.example"),
    ("CHANGE-ME-IN-PRODUCTION",   "same value, upper case"),
    ("  change-me-in-production ", "same value, padded"),
    ("your-secret-here-and-then-some-padding", "your-secret template"),
    ("replace_me_with_a_real_key_please_ok",   "replace_me template"),
    ("PLACEHOLDER-PLACEHOLDER-PLACEHOLDER",    "placeholder"),
    ("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",   "xxxx filler"),
    ("short",                     "far too short"),
    ("a" * 31,                    "one character under the minimum"),
])
def test_weak_keys_are_rejected(signer, value, why):
    with pytest.raises(ValueError) as exc:
        signer.reject_weak_key(value)
    # The message has to tell an operator what to do, not just that it failed.
    assert "openssl rand -hex 32" in str(exc.value), why


def test_rejection_names_the_variable(signer):
    """The error must identify which setting is wrong — it is raised at import,
    where there is no other context to go on."""
    with pytest.raises(ValueError, match="AUDIT_SIGNING_KEY"):
        signer.reject_weak_key("change-me-in-production")
    with pytest.raises(ValueError, match="SOME_OTHER_KEY"):
        signer.reject_weak_key("change-me-in-production", var_name="SOME_OTHER_KEY")


# ── Values that must be accepted ─────────────────────────────────────────────

@pytest.mark.parametrize("value, why", [
    ("a" * 32,                                 "exactly the minimum length"),
    ("f3c1" * 16,                              "64 hex characters, as openssl rand -hex 32 produces"),
    ("My-Org-Audit-Secret-2026-Rotation-01",   "operator passphrase containing the word 'secret'"),
    ("aTestingKeyWithEnoughLengthToPass12345", "contains 'test'"),
    ("passwordless-audit-key-with-enough-len", "contains 'password'"),
])
def test_legitimate_keys_are_accepted(signer, value, why):
    """A false positive here stops a correctly configured deployment from
    booting, so the marker list must stay narrow enough to let these through."""
    assert signer.reject_weak_key(value) == value.strip(), why


def test_returned_key_is_stripped(signer):
    assert signer.reject_weak_key("  " + "b" * 40 + "  ") == "b" * 40


# ── The signing round trip still works ───────────────────────────────────────

def test_sign_and_verify_round_trip(signer):
    event = {"action": "login", "user": "someone@example.com", "ts": "2026-01-01T00:00:00"}
    sig = signer.sign_event(event)
    assert signer.verify_event(event, sig) is True


def test_tampered_event_fails_verification(signer):
    event = {"action": "login", "user": "someone@example.com", "ts": "2026-01-01T00:00:00"}
    sig = signer.sign_event(event)
    tampered = dict(event, action="delete_everything")
    assert signer.verify_event(tampered, sig) is False


def test_signature_is_excluded_from_its_own_hash(signer):
    """sign_event must ignore an existing `signature` field, or verifying a row
    read back from the database — which carries one — would never match."""
    event = {"action": "read", "ts": "2026-01-01T00:00:00"}
    sig = signer.sign_event(event)
    assert signer.verify_event(dict(event, signature=sig), sig) is True
