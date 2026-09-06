# SPDX-License-Identifier: MIT
# ============================================================
# tests/core/ckms/test_bootstrap.py — load_at_boot() orchestration
#
# Repository and HSM gateway are both mocked so these tests don't need a
# database or a live HSM.
# ============================================================

from __future__ import annotations

import base64
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.ckms import bootstrap as bootstrap_mod
from core.ckms.key_service import KeyService

# repository imports SQLAlchemy; stub the KeyRow dataclass so these tests
# don't require a live DB or the sqlalchemy package to be installed.
try:
    from core.ckms.repository import KeyRow  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - dev convenience
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class KeyRow:  # type: ignore[no-redef]
        key_name: str
        dek: str
        kek: str
        status: str


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


@pytest.fixture(autouse=True)
def _isolate_protected_env(monkeypatch):
    """Strip every protected env var so an outer shell's plaintext values
    (e.g. ANTHROPIC_API_KEY exported for dev) don't make load_at_boot abort
    when it sees them as malformed ciphertext.

    Individual tests re-set just the vars they want exercised.
    """
    for v in bootstrap_mod.PROTECTED_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    for v in bootstrap_mod.DB_BOOTSTRAP_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("CKMS_BOOTSTRAP_DEK", raising=False)
    monkeypatch.delenv("CKMS_BOOTSTRAP_KEK", raising=False)
    yield


# ------------------------------------------------------------------
# Happy path — BASE: rows only (no HSM needed)
# ------------------------------------------------------------------

def _install_fake_repo(monkeypatch, rows, mapping):
    """Inject a stub ``core.ckms.repository`` module so bootstrap's late import
    succeeds without needing SQLAlchemy or a live DB.
    """
    import sys
    import types

    fake = types.ModuleType("core.ckms.repository")
    fake.load_active_keys = lambda: list(rows)
    fake.load_env_var_mapping = lambda: dict(mapping)
    fake.KeyRow = KeyRow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.ckms.repository", fake)


def test_base_only_boot_decrypts_protected_env_vars(monkeypatch):
    # 32-byte DEK presented to the boot path as BASE:<b64>.
    key_creds = os.urandom(32)
    token_creds = os.urandom(32)

    base_key_creds_row = KeyRow(
        key_name="KEY_CREDS",
        dek="BASE:" + base64.b64encode(key_creds).decode("ascii"),
        kek="",
        status="A",
    )
    base_token_creds_row = KeyRow(
        key_name="TOKEN_CREDS",
        dek="BASE:" + base64.b64encode(token_creds).decode("ascii"),
        kek="",
        status="A",
    )

    _install_fake_repo(
        monkeypatch,
        rows=[base_key_creds_row, base_token_creds_row],
        mapping={"GITLAB_TOKEN": "TOKEN_CREDS"},
    )

    # Seed two protected env vars in ciphertext form, ENC:-prefixed.
    monkeypatch.setenv("FERNET_KEY", "ENC:" + _encrypt("plain-fernet", key_creds))
    monkeypatch.setenv("GITLAB_TOKEN", "ENC:" + _encrypt("plain-gitlab-pat", token_creds))

    bootstrap_mod.load_at_boot()

    # os.environ has been mutated to the plaintext.
    assert os.environ["FERNET_KEY"] == "plain-fernet"
    assert os.environ["GITLAB_TOKEN"] == "plain-gitlab-pat"

    # Singleton is loaded; a second call is a no-op.
    assert KeyService.instance().loaded
    bootstrap_mod.load_at_boot()


# ------------------------------------------------------------------
# Failure paths — fail-fast with SystemExit(1)
# ------------------------------------------------------------------

def test_empty_keys_table_aborts(monkeypatch):
    _install_fake_repo(monkeypatch, rows=[], mapping={})
    with pytest.raises(SystemExit) as exc_info:
        bootstrap_mod.load_at_boot()
    assert exc_info.value.code == 1


def test_invalid_base_payload_aborts(monkeypatch):
    bad_row = KeyRow(
        key_name="KEY_CREDS",
        dek="BASE:!!!not-valid-base64!!!",
        kek="",
        status="A",
    )
    _install_fake_repo(monkeypatch, rows=[bad_row], mapping={})
    with pytest.raises(SystemExit) as exc_info:
        bootstrap_mod.load_at_boot()
    assert exc_info.value.code == 1


def test_malformed_env_ciphertext_aborts(monkeypatch):
    key_creds = os.urandom(32)
    row = KeyRow(
        key_name="KEY_CREDS",
        dek="BASE:" + base64.b64encode(key_creds).decode("ascii"),
        kek="",
        status="A",
    )
    _install_fake_repo(monkeypatch, rows=[row], mapping={})

    # ENC: prefix opts the value in for decryption, but the body has no ':'
    # separator → CipherFormatError → KeyServiceError → SystemExit(1).
    monkeypatch.setenv("FERNET_KEY", "ENC:not-a-valid-ciphertext")

    with pytest.raises(SystemExit) as exc_info:
        bootstrap_mod.load_at_boot()
    assert exc_info.value.code == 1


def test_wrong_key_tag_mismatch_aborts(monkeypatch):
    real_key = os.urandom(32)
    other_key = os.urandom(32)
    # Persisted row has `other_key`, but the ciphertext was made with `real_key`.
    row = KeyRow(
        key_name="KEY_CREDS",
        dek="BASE:" + base64.b64encode(other_key).decode("ascii"),
        kek="",
        status="A",
    )
    _install_fake_repo(monkeypatch, rows=[row], mapping={})

    monkeypatch.setenv("FERNET_KEY", "ENC:" + _encrypt("foo", real_key))

    with pytest.raises(SystemExit) as exc_info:
        bootstrap_mod.load_at_boot()
    assert exc_info.value.code == 1


# ------------------------------------------------------------------
# HSM path — verify gateway is called for non-BASE rows
# ------------------------------------------------------------------

def test_non_base_row_invokes_hsm_gateway(monkeypatch):
    expected_dek = os.urandom(32)
    calls = []

    class _FakeGateway:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def unwrap_dek(self, dek, kek):
            calls.append((dek, kek))
            return expected_dek

    monkeypatch.setattr(bootstrap_mod, "HSMGateway", _FakeGateway)

    wire_row = KeyRow(
        key_name="KEY_CREDS",
        dek="AABBCC112233",   # not BASE:-prefixed → HSM call
        kek="DDEEFF445566",
        status="A",
    )
    _install_fake_repo(monkeypatch, rows=[wire_row], mapping={})

    hsm_fernet_fixture = os.environ.get("TEST_HSM_FERNET_VAL", "hsm-fernet-test-val")
    monkeypatch.setenv("FERNET_KEY", "ENC:" + _encrypt(hsm_fernet_fixture, expected_dek))

    bootstrap_mod.load_at_boot()

    assert calls == [("AABBCC112233", "DDEEFF445566")]
    assert os.environ["FERNET_KEY"] == hsm_fernet_fixture


# ------------------------------------------------------------------
# ENC: prefix backward-compatibility — plain values pass through.
# ------------------------------------------------------------------

def _base_row(key_creds: bytes) -> KeyRow:
    return KeyRow(
        key_name="KEY_CREDS",
        dek="BASE:" + base64.b64encode(key_creds).decode("ascii"),
        kek="",
        status="A",
    )


def test_plaintext_env_var_passes_through_unchanged(monkeypatch):
    """No ENC: prefix → value left as-is, no decrypt attempted."""
    key_creds = os.urandom(32)
    _install_fake_repo(monkeypatch, rows=[_base_row(key_creds)], mapping={})

    monkeypatch.setenv("POSTGRES_PASSWORD", "legacy-plain-password")

    bootstrap_mod.load_at_boot()

    assert os.environ["POSTGRES_PASSWORD"] == "legacy-plain-password"


def test_mixed_env_vars_partial_rollout(monkeypatch):
    """Some vars ENC:-prefixed, others plaintext — both modes coexist."""
    key_creds = os.urandom(32)
    _install_fake_repo(monkeypatch, rows=[_base_row(key_creds)], mapping={})

    monkeypatch.setenv("FERNET_KEY", "ENC:" + _encrypt("decrypted-fernet", key_creds))
    plain_postgres_fixture = os.environ.get("TEST_PG_PLAIN_VAL", "still-plain-db-val")
    monkeypatch.setenv("POSTGRES_PASSWORD", plain_postgres_fixture)
    monkeypatch.setenv("REDIS_PASSWORD", "still-plain-redis")

    bootstrap_mod.load_at_boot()

    # ENC: var was decrypted; plain vars untouched.
    assert os.environ["FERNET_KEY"] == "decrypted-fernet"
    assert os.environ["POSTGRES_PASSWORD"] == plain_postgres_fixture
    assert os.environ["REDIS_PASSWORD"] == "still-plain-redis"


def test_bogus_plaintext_does_not_brick_boot(monkeypatch):
    """A garbled non-ENC value is treated as legacy plaintext — no abort.

    This is the safety net: if ops mis-types a value but forgets the ENC:
    prefix, CKMS will NOT crash the gateway. The downstream consumer
    (Postgres, JWT verify, etc.) raises a narrow error later.
    """
    key_creds = os.urandom(32)
    _install_fake_repo(monkeypatch, rows=[_base_row(key_creds)], mapping={})

    monkeypatch.setenv("POSTGRES_PASSWORD", "::::not-a-ciphertext::::")

    bootstrap_mod.load_at_boot()  # must NOT SystemExit

    assert os.environ["POSTGRES_PASSWORD"] == "::::not-a-ciphertext::::"


def test_enc_prefix_alone_is_treated_as_ciphertext(monkeypatch):
    """An ENC: prefix with empty body fails fast — caller opted in."""
    key_creds = os.urandom(32)
    _install_fake_repo(monkeypatch, rows=[_base_row(key_creds)], mapping={})

    monkeypatch.setenv("FERNET_KEY", "ENC:")

    with pytest.raises(SystemExit) as exc_info:
        bootstrap_mod.load_at_boot()
    assert exc_info.value.code == 1


# ------------------------------------------------------------------
# Step 0 — env-sourced bootstrap DEK decrypts DB-connectivity vars
# BEFORE keys_table is read.
# ------------------------------------------------------------------

def test_step0_decrypts_db_password_before_repo_is_read(monkeypatch):
    """The ENC: POSTGRES_PASSWORD is decrypted with CKMS_BOOTSTRAP_DEK
    before any DB connection is attempted."""
    boot_dek = os.urandom(32)
    key_creds = os.urandom(32)

    # The repo fake will assert that POSTGRES_PASSWORD is already plaintext
    # when load_active_keys() is called — proving Step 0 ran first.
    seen = {}

    def _fake_load_active_keys():
        seen["pg_pw_at_repo_call"] = os.environ.get("POSTGRES_PASSWORD")
        return [_base_row(key_creds)]

    import sys
    import types

    fake = types.ModuleType("core.ckms.repository")
    fake.load_active_keys = _fake_load_active_keys
    fake.load_env_var_mapping = lambda: {}
    fake.KeyRow = KeyRow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.ckms.repository", fake)

    monkeypatch.setenv(
        "CKMS_BOOTSTRAP_DEK",
        "BASE:" + base64.b64encode(boot_dek).decode("ascii"),
    )
    monkeypatch.setenv(
        "POSTGRES_PASSWORD",
        "ENC:" + _encrypt("super-secret-pg-pwd", boot_dek),
    )

    bootstrap_mod.load_at_boot()

    assert seen["pg_pw_at_repo_call"] == "super-secret-pg-pwd"
    assert os.environ["POSTGRES_PASSWORD"] == "super-secret-pg-pwd"


def test_step0_required_only_when_db_var_is_enc_prefixed(monkeypatch):
    """No DB var ENC:-prefixed → bootstrap DEK not required."""
    key_creds = os.urandom(32)
    _install_fake_repo(monkeypatch, rows=[_base_row(key_creds)], mapping={})

    # POSTGRES_PASSWORD is legacy plaintext, no bootstrap DEK provisioned.
    legacy_postgres_fixture = os.environ.get("TEST_PG_LEGACY_VAL", "legacy-plain-db-val")
    monkeypatch.setenv("POSTGRES_PASSWORD", legacy_postgres_fixture)
    # CKMS_BOOTSTRAP_DEK deliberately absent — Step 0 must no-op.

    bootstrap_mod.load_at_boot()  # must NOT exit

    assert os.environ["POSTGRES_PASSWORD"] == legacy_postgres_fixture


def test_step0_missing_dek_aborts_when_db_var_is_enc(monkeypatch):
    """At least one DB var is ENC: but CKMS_BOOTSTRAP_DEK not set → abort."""
    key_creds = os.urandom(32)
    _install_fake_repo(monkeypatch, rows=[_base_row(key_creds)], mapping={})

    boot_dek = os.urandom(32)  # never persisted to env
    monkeypatch.setenv(
        "POSTGRES_PASSWORD",
        "ENC:" + _encrypt("does-not-matter", boot_dek),
    )

    with pytest.raises(SystemExit) as exc_info:
        bootstrap_mod.load_at_boot()
    assert exc_info.value.code == 1


def test_step0_wrong_bootstrap_dek_aborts(monkeypatch):
    """Bootstrap DEK is set but doesn't match the one used to encrypt
    POSTGRES_PASSWORD → GCM tag mismatch → abort."""
    key_creds = os.urandom(32)
    _install_fake_repo(monkeypatch, rows=[_base_row(key_creds)], mapping={})

    real_boot_dek = os.urandom(32)
    other_boot_dek = os.urandom(32)
    monkeypatch.setenv(
        "CKMS_BOOTSTRAP_DEK",
        "BASE:" + base64.b64encode(other_boot_dek).decode("ascii"),
    )
    monkeypatch.setenv(
        "POSTGRES_PASSWORD",
        "ENC:" + _encrypt("pg-pwd", real_boot_dek),
    )

    with pytest.raises(SystemExit) as exc_info:
        bootstrap_mod.load_at_boot()
    assert exc_info.value.code == 1


def test_step0_covers_all_db_connectivity_vars(monkeypatch):
    """All five DB-connectivity vars get decrypted in Step 0 when ENC:."""
    boot_dek = os.urandom(32)
    key_creds = os.urandom(32)
    _install_fake_repo(monkeypatch, rows=[_base_row(key_creds)], mapping={})

    monkeypatch.setenv(
        "CKMS_BOOTSTRAP_DEK",
        "BASE:" + base64.b64encode(boot_dek).decode("ascii"),
    )
    monkeypatch.setenv("POSTGRES_PASSWORD",         "ENC:" + _encrypt("pg-rw",   boot_dek))
    monkeypatch.setenv("POSTGRES_READ_PASSWORD",    "ENC:" + _encrypt("pg-ro",   boot_dek))
    monkeypatch.setenv("POSTGRES_MIGRATE_PASSWORD", "ENC:" + _encrypt("pg-mig",  boot_dek))
    monkeypatch.setenv("PGVECTOR_PASSWORD",         "ENC:" + _encrypt("vec-rw",  boot_dek))
    monkeypatch.setenv("PGVECTOR_READ_PASSWORD",    "ENC:" + _encrypt("vec-ro",  boot_dek))

    bootstrap_mod.load_at_boot()

    assert os.environ["POSTGRES_PASSWORD"]         == "pg-rw"
    assert os.environ["POSTGRES_READ_PASSWORD"]    == "pg-ro"
    assert os.environ["POSTGRES_MIGRATE_PASSWORD"] == "pg-mig"
    assert os.environ["PGVECTOR_PASSWORD"]         == "vec-rw"
    assert os.environ["PGVECTOR_READ_PASSWORD"]    == "vec-ro"
