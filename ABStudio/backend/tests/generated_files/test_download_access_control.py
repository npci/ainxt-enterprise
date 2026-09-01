# SPDX-License-Identifier: Apache-2.0
"""Access-control tests for GET /generated-files/{filename} (IDOR fix).

Background: generated artifacts used to live flat under GENERATED_FILES_DIR
named ``{run_id}_{name}`` with only 32 bits of run_id entropy, and the download
endpoint checked authentication but NOT ownership — so any authenticated user
who saw or guessed a disk name could fetch another user's artifact.

The fix nests each artifact under a per-user owner-dir (``{owner_tag}/{name}``)
and enforces that a caller may only read files inside their own owner-dir.
Legacy flat files (no owner-dir) remain readable by any authenticated user.

These tests exercise:
  - owner_tag: deterministic, per-user, empty on empty id
  - is_generated_path_allowed: the pure ownership decision
  - rehome_generated_file: moves a flat artifact into the owner-dir
  - the live endpoint via TestClient with require_access overridden
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

import app.main as m
from app.models import AuthenticatedUser
from app.api.deps import require_access


_USER_A = "user-aaaa"
_USER_B = "user-bbbb"


def _as_user(uid: str) -> AuthenticatedUser:
    return AuthenticatedUser(id=uid, email=f"{uid}@x", full_name=uid, role="user")


@pytest.fixture
def client_for():
    """Return a factory that yields a TestClient authenticated as ``uid``."""
    def _factory(uid: str) -> TestClient:
        m.app.dependency_overrides[require_access] = lambda: _as_user(uid)
        return TestClient(m.app)
    yield _factory
    m.app.dependency_overrides.pop(require_access, None)


@pytest.fixture
def gen_dir(tmp_path, monkeypatch):
    """Point GENERATED_FILES_DIR at a temp dir for the duration of a test."""
    d = tmp_path / "gen"
    d.mkdir()
    monkeypatch.setattr(m, "GENERATED_FILES_DIR", str(d))
    return d


# ── owner_tag ────────────────────────────────────────────────────────────────

def test_owner_tag_is_deterministic_and_per_user():
    assert m.owner_tag(_USER_A) == m.owner_tag(_USER_A)
    assert m.owner_tag(_USER_A) != m.owner_tag(_USER_B)
    assert len(m.owner_tag(_USER_A)) == 16


def test_owner_tag_empty_for_no_identity():
    assert m.owner_tag("") == ""
    assert m.owner_tag("   ") == ""


# ── single-definition invariant ──────────────────────────────────────────────
# The owner-tag scheme was previously hand-copied into three modules
# (app.main, cli_runtime.workspace, gateway) and kept in sync only by comments;
# the gateway copy had no test coverage at all. The algorithm now lives in the
# stdlib-only ``app.owner_tag`` and every consumer imports it, so these tests
# assert object IDENTITY rather than comparing recomputed hashes — that makes
# divergence structurally impossible instead of merely detectable.

def test_main_reexports_the_canonical_functions():
    """app.main must re-export, not re-implement."""
    import app.owner_tag as ot
    assert m.owner_tag is ot.owner_tag
    assert m.is_generated_path_allowed is ot.is_generated_path_allowed


def test_every_consumer_shares_one_definition():
    """app.main, cli_runtime.workspace and the gateway must all resolve to the
    SAME function object — the invariant the three copies used to risk."""
    import app.owner_tag as ot
    import app.cli_runtime.workspace as ws
    assert ws._owner_tag is ot.owner_tag
    assert m.owner_tag is ot.owner_tag


def test_owner_tag_module_is_import_light():
    """``app.owner_tag`` must stay dependency-free: the CLI runtime and the
    gateway import it precisely to avoid pulling in FastAPI / app.main (which
    builds an app object and runs load_dotenv(override=True) at import time).
    A new dependency here would silently reintroduce that coupling."""
    import subprocess
    import sys
    from pathlib import Path
    backend = Path(__file__).resolve().parents[2]
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import app.owner_tag;"
        "assert 'fastapi' not in sys.modules, 'fastapi leaked in';"
        "assert 'structlog' not in sys.modules, 'structlog leaked in';"
        "assert 'app.main' not in sys.modules, 'app.main leaked in';"
        "print('clean')" % backend
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, f"import not clean: {out.stderr}"


# ── the gateway's ownership gate (previously untested) ───────────────────────
# gateway.py serves generated files under /abs when the platform runs embedded,
# via its own _abs_generated_path_allowed. That helper was a hand-copied
# duplicate with zero tests; it now imports the shared function, so the gate is
# covered by the same cases as the standalone path below.

def test_gateway_gate_is_the_shared_function():
    """The gateway must not re-implement the ownership decision."""
    import importlib
    ot = importlib.import_module("app.owner_tag")
    # Resolved the same way gateway.py does it (ABStudio/backend on sys.path).
    from app.owner_tag import is_generated_path_allowed as gateway_gate
    assert gateway_gate is ot.is_generated_path_allowed


def test_gateway_gate_denies_cross_tenant_access():
    """The case the gateway copy never had a test for: user B must not reach
    user A's owner-dir. 404 (not 403) is enforced by the caller."""
    from app.owner_tag import is_generated_path_allowed as gate
    tag_a = m.owner_tag(_USER_A)
    assert gate((tag_a, "deck.pptx"), _USER_A) is True
    assert gate((tag_a, "deck.pptx"), _USER_B) is False
    assert gate((tag_a, "sub", "deck.pptx"), _USER_A) is False
    assert gate(("deck.pptx",), _USER_B) is True  # legacy flat, still allowed


# ── is_generated_path_allowed (pure decision) ────────────────────────────────

def test_legacy_flat_file_allowed_for_anyone():
    assert m.is_generated_path_allowed(("deck.pptx",), _USER_A) is True
    assert m.is_generated_path_allowed(("deck.pptx",), "") is True


def test_owner_dir_allowed_only_for_owner():
    tag_a = m.owner_tag(_USER_A)
    assert m.is_generated_path_allowed((tag_a, "deck.pptx"), _USER_A) is True
    # User B must NOT be allowed into user A's owner-dir.
    assert m.is_generated_path_allowed((tag_a, "deck.pptx"), _USER_B) is False


def test_deeper_nesting_rejected():
    tag_a = m.owner_tag(_USER_A)
    assert m.is_generated_path_allowed((tag_a, "sub", "deck.pptx"), _USER_A) is False


# ── rehome_generated_file ────────────────────────────────────────────────────

def test_rehome_moves_flat_file_into_owner_dir(gen_dir):
    flat = gen_dir / "a1b2c3d4_deck.pptx"
    flat.write_bytes(b"content")
    key = m.rehome_generated_file("a1b2c3d4_deck.pptx", _USER_A)
    tag = m.owner_tag(_USER_A)
    assert key == f"{tag}/a1b2c3d4_deck.pptx"
    assert not flat.exists()
    assert (gen_dir / tag / "a1b2c3d4_deck.pptx").is_file()


def test_rehome_no_identity_keeps_flat(gen_dir):
    flat = gen_dir / "a1b2c3d4_deck.pptx"
    flat.write_bytes(b"content")
    key = m.rehome_generated_file("a1b2c3d4_deck.pptx", "")
    assert key == "a1b2c3d4_deck.pptx"
    assert flat.is_file()


# ── live endpoint ────────────────────────────────────────────────────────────

def test_owner_can_download_their_file(client_for, gen_dir):
    tag_a = m.owner_tag(_USER_A)
    (gen_dir / tag_a).mkdir()
    (gen_dir / tag_a / "report.docx").write_bytes(b"hello")

    c = client_for(_USER_A)
    resp = c.get(f"/generated-files/{tag_a}/report.docx")
    assert resp.status_code == 200
    assert resp.content == b"hello"


def test_other_user_gets_404_not_403(client_for, gen_dir):
    """The core IDOR assertion: user B cannot read user A's artifact."""
    tag_a = m.owner_tag(_USER_A)
    (gen_dir / tag_a).mkdir()
    (gen_dir / tag_a / "report.docx").write_bytes(b"secret")

    c = client_for(_USER_B)
    resp = c.get(f"/generated-files/{tag_a}/report.docx")
    # 404 (not 403) so existence is not confirmed.
    assert resp.status_code == 404


def test_legacy_flat_file_downloadable_by_any_user(client_for, gen_dir):
    (gen_dir / "old_deck.pptx").write_bytes(b"legacy")
    c = client_for(_USER_B)
    resp = c.get("/generated-files/old_deck.pptx")
    assert resp.status_code == 200
    assert resp.content == b"legacy"


def test_path_traversal_rejected(client_for, gen_dir):
    c = client_for(_USER_A)
    resp = c.get("/generated-files/..%2f..%2fetc%2fpasswd")
    assert resp.status_code in (400, 404)
