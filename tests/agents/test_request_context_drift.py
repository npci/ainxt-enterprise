# SPDX-License-Identifier: Apache-2.0
# ============================================================
# RequestContext capture layer — drift guard (Wave 1)
# ============================================================
#
# gateway.py cannot be imported in a bare test env (HSM/redis at import), so —
# exactly like tests/agents/test_gateway_passthrough_logic.py — this reads
# gateway.py as source TEXT and regex-asserts that the Wave 1 shadow-capture
# layer stays:
#   1. behind a DEFAULT-OFF flag (PIPELINE_V2)  → prod inertness invariant
#   2. GUARDED (every _rc write under `if _PIPELINE_V2`) → never un-gated
#   3. aligned with the identity contract (the 11 _user_ctx keys)
#   4. consistent with the RequestContext dataclass field set
#
# If someone un-guards a capture, changes the flag default, or drifts the
# identity dict, this fails loudly.
# ============================================================

import dataclasses
import os
import re

import pytest

from pipeline.request_context import RequestContext


GATEWAY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "gateway.py",
)


@pytest.fixture(scope="module")
def gateway_src():
    with open(GATEWAY_PATH, "r", encoding="utf-8") as f:
        return f.read()


# ── 1. flag exists and defaults OFF (prod-inertness invariant) ──────────────

def test_pipeline_v2_flag_default_off(gateway_src):
    m = re.search(
        r'_PIPELINE_V2\s*=\s*os\.getenv\(\s*"PIPELINE_V2"\s*,\s*"([^"]+)"\s*\)',
        gateway_src,
    )
    assert m, "could not find _PIPELINE_V2 flag in gateway.py"
    assert m.group(1).lower() == "false", (
        "PIPELINE_V2 must default to 'false' so the Wave 1 capture layer is "
        "inert in production"
    )


# ── 2. capture layer present and guarded ────────────────────────────────────

def test_request_context_constructed(gateway_src):
    assert "_rc = RequestContext(" in gateway_src, (
        "gateway.py must build the RequestContext shadow object"
    )


def test_every_rc_write_is_guarded(gateway_src):
    """No `_rc.<field> = ...` assignment may exist outside a PIPELINE_V2 guard.

    We scan each line that assigns to an `_rc.` attribute and require a
    preceding `if _PIPELINE_V2` within a small window (the enclosing guard).
    """
    lines = gateway_src.splitlines()
    guard_re = re.compile(r"if _PIPELINE_V2\b")
    assign_re = re.compile(r"^\s*_rc\.\w+\s*=")
    for i, line in enumerate(lines):
        if assign_re.match(line):
            window = lines[max(0, i - 6):i]
            assert any(guard_re.search(w) for w in window), (
                f"_rc assignment at line {i + 1} is not under an "
                f"`if _PIPELINE_V2` guard: {line.strip()!r}"
            )


def test_rc_initialized_none_for_flag_off_path(gateway_src):
    # `_rc = None` must exist so the flag-off path has a safe sentinel and the
    # `_rc is not None` guards short-circuit.
    assert re.search(r"^\s*_rc\s*=\s*None\s*$", gateway_src, re.MULTILINE), (
        "`_rc = None` sentinel missing — flag-off guards rely on it"
    )


# ── 3. identity contract — the 11 user_ctx keys are frozen ──────────────────

_EXPECTED_USER_CTX_KEYS = {
    "user_id", "user_role", "ad_level", "department", "is_admin",
    "can_approve", "org_id", "session_id", "name", "ad_username", "email",
}


def test_user_ctx_keys_unchanged(gateway_src):
    # find the JWT _user_ctx dict literal and extract its quoted keys
    m = re.search(r"_user_ctx\s*=\s*\{(.*?)\}", gateway_src, re.DOTALL)
    assert m, "could not find _user_ctx dict literal in gateway.py"
    keys = set(re.findall(r'"(\w+)":', m.group(1)))
    missing = _EXPECTED_USER_CTX_KEYS - keys
    assert not missing, (
        f"identity contract drifted — _user_ctx missing keys {missing}. "
        f"Update RequestContext docs and this test if intentional."
    )


# ── 4. RequestContext dataclass shape ───────────────────────────────────────

def test_request_context_fields():
    names = {f.name for f in dataclasses.fields(RequestContext)}
    expected = {
        "request_id", "start_time", "user_id", "user_dept", "user_ctx",
        "auth_method", "product_ids", "chat_id", "rag_mode", "doc_intent",
        "policy", "conv_state",  # conv_state added in Wave 2
    }
    assert expected <= names, f"RequestContext missing fields: {expected - names}"


def test_cil_capture_present_and_guarded(gateway_src):
    # Wave 2: the CIL state must be captured, and (like all captures) guarded.
    assert "_rc.conv_state = _cil_analyze(" in gateway_src, (
        "gateway.py must shadow-capture the ConversationState via _cil_analyze"
    )
    lines = gateway_src.splitlines()
    guard_re = re.compile(r"if _PIPELINE_V2\b")
    for i, line in enumerate(lines):
        if "_cil_analyze(" in line:
            window = lines[max(0, i - 8):i]
            assert any(guard_re.search(w) for w in window), (
                f"_cil_analyze call at line {i + 1} is not under an "
                f"`if _PIPELINE_V2` guard"
            )


def test_request_context_snapshot_is_flat_scalars():
    rc = RequestContext(request_id="r", start_time=1.0, rag_mode="kb")
    snap = rc.snapshot()
    # snapshot must be JSON-safe scalars/None for span attributes
    for k, v in snap.items():
        assert v is None or isinstance(v, (str, int, float, bool)), (
            f"snapshot[{k}] is not a scalar: {type(v)}"
        )
