# SPDX-License-Identifier: MIT
# ============================================================
# gateway._pt_ledger_* — passthrough compliance scan ledger.
#
# WHY THIS TEST EXISTS
# --------------------
# The browser-agent passthrough lane skips re-scanning history messages that
# already passed the compliance block-gate (F1 in
# browser-agent-latency-fix_req.md), bounding ML calls to O(N) per session.
#
# The SUBTLE correctness requirement: on the skip path the upstream payload must
# be BYTE-IDENTICAL to a fresh scan. An earlier design reused mask_pii(raw) on
# the skip path, but:
#
#   - validate_input().redacted_text redacts regex-detected AND ML-detected
#     values (agents/compliance_engine.py:464-509), e.g. an ML-only-detected
#     ACCOUNT_NUMBER.
#   - mask_pii() (gateway.py:1822) is REGEX-ONLY (cards/phones/email/PAN).
#
# So mask_pii on the skip path would leak an ML-only-detected value in the clear
# from turn 2 onward. The fix stores the exact redacted_text in the ledger and
# reuses it. These tests pin:
#   1. the ledger stores/returns the exact redacted text (not a bool),
#   2. mask_pii and validate_input DIVERGE for ML-only values (the reason the
#      skip path must NOT fall back to mask_pii),
#   3. the key is salted per-user (one user can't skip another's message),
#   4. bounded LRU eviction.
#
# WHY WE exec() THE SOURCE INSTEAD OF `import gateway`
# ---------------------------------------------------
# gateway.py runs core.ckms.load_at_boot() at import time, which needs a live
# HSM unavailable in CI. We load ONLY the ledger symbols + mask_pii from the
# real source into an isolated namespace (same technique as
# test_browser_agent_prompt.py), so we still test the shipped code.
# ============================================================

from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest

_GATEWAY_PATH = pathlib.Path(__file__).resolve().parent.parent / "gateway.py"


def _load_ledger_symbols():
    """Extract the ledger helpers + mask_pii + mask regexes from gateway.py
    source without importing the module (which would run HSM boot code)."""
    source = _GATEWAY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    wanted_funcs = {
        "_pt_ledger_key",
        "_pt_ledger_get",
        "_pt_ledger_mark",
        "_pt_scan_tool_result",
        "mask_pii",
    }
    wanted_assigns = {
        "_PT_SCAN_LEDGER_MAX",
        "_PT_SCAN_LEDGER_ENABLED",
        "_pt_scan_ledger",
        "_pt_scan_ledger_lock",
        "_MASK_CARD16_RE",
        "_MASK_PHONE_RE",
        "_MASK_EMAIL_RE",
        "_MASK_IPAN_RE",
    }
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & wanted_assigns:
                selected.append(node)
        elif isinstance(node, ast.AnnAssign):
            # e.g.  _pt_scan_ledger: "_PTOrderedDict[str, str]" = OrderedDict()
            if isinstance(node.target, ast.Name) and node.target.id in wanted_assigns:
                selected.append(node)
        elif isinstance(node, ast.ImportFrom) and node.module == "collections":
            selected.append(node)

    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    # Provide the module-level deps the extracted code closes over.
    namespace: dict = {"os": os, "re": re}
    import hashlib
    import threading
    namespace["hashlib"] = hashlib
    namespace["threading"] = threading
    exec(compile(module, str(_GATEWAY_PATH), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture()
def gw():
    ns = _load_ledger_symbols()
    # Start each test with a clean ledger.
    ns["_pt_scan_ledger"].clear()
    return ns


# ---------------------------------------------------------------------------
# 1. The ledger stores and returns the EXACT redacted text (not a bool).
# ---------------------------------------------------------------------------
def test_ledger_roundtrips_exact_redacted_text(gw):
    key = gw["_pt_ledger_key"]("user-A", "tool", "raw tool output")
    redacted = "account 12******89 balance shown"  # what validate_input produced

    assert gw["_pt_ledger_get"](key) is None          # miss before mark
    gw["_pt_ledger_mark"](key, redacted)
    assert gw["_pt_ledger_get"](key) == redacted       # exact reuse, not a bool


def test_ledger_stores_empty_string_as_valid_hit(gw):
    """A fully-redacted result can be an empty string; that must count as a HIT
    (get returns "" which `is not None`), NOT a miss that triggers a re-scan."""
    key = gw["_pt_ledger_key"]("user-A", "tool", "everything sensitive")
    gw["_pt_ledger_mark"](key, "")
    assert gw["_pt_ledger_get"](key) == ""
    assert gw["_pt_ledger_get"](key) is not None


# ---------------------------------------------------------------------------
# 2. The core regression: mask_pii != validate_input().redacted_text for an
#    ML-only value, which is EXACTLY why the skip path must reuse the stored
#    redacted_text instead of calling mask_pii(raw).
# ---------------------------------------------------------------------------
def test_maskpii_does_not_cover_ml_only_account_number(gw):
    """mask_pii is regex-only: a bare 11-digit account number (no card/phone/
    email/PAN shape) passes through UNMASKED. Reusing mask_pii on the skip path
    would therefore leak it — so the ledger must store the ML redacted_text."""
    raw = "account balance for 50100234567 is 1200"
    masked = gw["mask_pii"](raw)
    assert "50100234567" in masked, (
        "If mask_pii ever starts masking bare account numbers, revisit whether "
        "the ledger still needs to store redacted_text — but today it does."
    )


def test_skip_path_reuses_ml_redaction_not_maskpii(gw):
    """Simulate the two turns: turn 1 does a full scan (ML redacts the account
    number); turn 2 hits the ledger. The reused value must keep the account
    number masked — proving we did NOT fall back to mask_pii."""
    raw = "account balance for 50100234567 is 1200"
    # What validate_input().redacted_text would return (ML masked the acct no).
    ml_redacted = "account balance for 50******567 is 1200"

    key = gw["_pt_ledger_key"]("user-A", "tool", raw)
    # Turn 1: fresh scan passed → store the ML redacted text.
    gw["_pt_ledger_mark"](key, ml_redacted)
    # Turn 2: skip-path reuse.
    reused = gw["_pt_ledger_get"](key)

    assert reused == ml_redacted
    assert "50100234567" not in reused          # NOT leaked
    assert gw["mask_pii"](raw) != reused         # mask_pii would have leaked it


# ---------------------------------------------------------------------------
# 3. Per-user salt: identical content for two users must NOT collide.
# ---------------------------------------------------------------------------
def test_key_is_salted_per_user(gw):
    text = "click login"
    ka = gw["_pt_ledger_key"]("user-A", "user", text)
    kb = gw["_pt_ledger_key"]("user-B", "user", text)
    assert ka != kb

    gw["_pt_ledger_mark"](ka, text)
    assert gw["_pt_ledger_get"](ka) == text
    assert gw["_pt_ledger_get"](kb) is None      # user B is not skipped


def test_key_distinguishes_role(gw):
    text = "same bytes"
    assert gw["_pt_ledger_key"]("u", "user", text) != gw["_pt_ledger_key"]("u", "tool", text)


def test_key_handles_none_user_id(gw):
    # _user_id can be falsy before resolution; must not raise.
    k = gw["_pt_ledger_key"](None, "tool", "x")
    assert isinstance(k, str) and len(k) == 64


# ---------------------------------------------------------------------------
# 4. Bounded LRU eviction.
# ---------------------------------------------------------------------------
def test_ledger_evicts_lru_when_over_capacity(gw, monkeypatch):
    # Shrink the cap for the test by patching the module-level constant the
    # function closes over via globals().
    gw["_PT_SCAN_LEDGER_MAX"] = 3
    mark = gw["_pt_ledger_mark"]
    get = gw["_pt_ledger_get"]
    key = gw["_pt_ledger_key"]

    keys = [key("u", "tool", f"m{i}") for i in range(4)]
    for k in keys[:3]:
        mark(k, "r")
    # Touch keys[0] so it becomes most-recently-used and survives eviction.
    assert get(keys[0]) == "r"
    mark(keys[3], "r")  # inserts 4th → evicts the LRU (keys[1])

    assert get(keys[0]) == "r"
    assert get(keys[1]) is None      # evicted (was LRU)
    assert get(keys[2]) == "r"
    assert get(keys[3]) == "r"


# ---------------------------------------------------------------------------
# 5. _pt_scan_tool_result — the compliance-critical decision helper.
#    These exercise the SHIPPED branch logic (extracted from
#    _build_passthrough_messages), not a re-implementation.
# ---------------------------------------------------------------------------
class _StubEngine:
    """Records calls; returns a scripted validate_input result."""

    def __init__(self, findings, redacted_text):
        self._findings = findings
        self._redacted = redacted_text
        self.calls = 0

    def validate_input(self, raw):
        self.calls += 1
        return {"findings": self._findings, "redacted_text": self._redacted}


def _mask_noop(raw):
    # Distinct from any redacted_text so we can prove which path produced output.
    return f"MASKED::{raw}"


def test_blocked_result_is_never_laddered_and_re_blocks(gw):
    """A tool result with block findings must NOT enter the ledger, so a later
    turn re-scans it and re-blocks — no silent un-block. This is the core PCI
    safety invariant."""
    scan = gw["_pt_scan_tool_result"]
    # TEST VECTOR: 4111111111111111 is the standard Luhn-valid Visa test card number.
    raw = "card 4111111111111111 leaked"
    engine = _StubEngine(
        findings=[{"type": "CARD", "blocked": True}],
        redacted_text="card [REDACTED] leaked",
    )

    # Turn 1 (current turn): scanned fresh, blocks.
    safe1, blocked1 = scan("user-A", raw, True, engine.validate_input, _mask_noop)
    assert blocked1 == ["CARD"]
    assert engine.calls == 1

    # Turn 2 (now history, not current): MUST re-scan (not in ledger) and re-block.
    safe2, blocked2 = scan("user-A", raw, False, engine.validate_input, _mask_noop)
    assert blocked2 == ["CARD"]         # re-blocked, not silently allowed
    assert engine.calls == 2            # proves it was re-scanned, not skipped
    assert gw["_pt_ledger_get"](gw["_pt_ledger_key"]("user-A", "tool", raw)) is None


def test_passed_result_is_laddered_and_skips_rescan(gw):
    """A result that passes is stored and, once it is history (not current),
    served from the ledger without another validate_input call."""
    scan = gw["_pt_scan_tool_result"]
    raw = "account 50100234567 balance"
    engine = _StubEngine(
        findings=[{"type": "ACCOUNT_NUMBER", "blocked": False}],
        redacted_text="account 50******567 balance",
    )

    # Turn 1: fresh scan, passes, stored.
    safe1, blocked1 = scan("user-A", raw, True, engine.validate_input, _mask_noop)
    assert blocked1 == []
    assert safe1 == "account 50******567 balance"
    assert engine.calls == 1

    # Turn 2 (history): skip path reuses the stored ML-redacted text.
    safe2, blocked2 = scan("user-A", raw, False, engine.validate_input, _mask_noop)
    assert blocked2 == []
    assert safe2 == "account 50******567 balance"   # NOT MASKED:: (mask_noop)
    assert "50100234567" not in safe2                # ML value not leaked
    assert engine.calls == 1                         # NOT re-scanned


def test_current_turn_is_always_scanned_fresh_even_if_laddered(gw):
    """Even if identical content is already in the ledger, the CURRENT turn is
    re-scanned so a newly-violating current turn can never be skipped."""
    scan = gw["_pt_scan_tool_result"]
    raw = "some tool output"
    engine = _StubEngine(findings=[{"type": "X", "blocked": False}],
                         redacted_text="redacted")

    scan("user-A", raw, False, engine.validate_input, _mask_noop)  # ladders it
    assert engine.calls == 1
    scan("user-A", raw, True, engine.validate_input, _mask_noop)   # current turn
    assert engine.calls == 2                                        # scanned again


def test_disabled_ledger_always_rescans(gw):
    """Kill-switch: with the ledger disabled, every call scans fresh (correct,
    just slower) and nothing is served from cache."""
    gw["_PT_SCAN_LEDGER_ENABLED"] = False
    scan = gw["_pt_scan_tool_result"]
    raw = "output"
    engine = _StubEngine(findings=[], redacted_text="output")

    scan("user-A", raw, False, engine.validate_input, _mask_noop)
    scan("user-A", raw, False, engine.validate_input, _mask_noop)
    assert engine.calls == 2
