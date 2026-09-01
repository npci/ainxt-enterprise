# SPDX-License-Identifier: Apache-2.0
# ============================================================
# FR-T0-3 (REQ-T2/T4) — signed webhook ingestion helpers
#
# Covers the pure security-relevant logic of the webhook route:
#   _verify_signature  — HMAC-SHA256 constant-time verification
#   _event_matches     — Jira / GitLab / generic event filtering
#   _rate_limited      — per-trigger burst back-pressure
#
# app.api.triggers lives under ABStudio/backend and pulls apscheduler +
# ABStudio config, so the whole module is skipped cleanly if those deps
# are missing (keeps partial local setups / CI green).
# ============================================================

import hashlib
import hmac
import os
import sys

import pytest

_ABS_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ABStudio", "backend")
)
if _ABS_BACKEND not in sys.path:
    sys.path.insert(0, _ABS_BACKEND)

triggers = pytest.importorskip(
    "app.api.triggers",
    reason="ABStudio backend deps (apscheduler/config) not installed",
)


# ── _verify_signature ─────────────────────────────────────────────────────

def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid_bare_digest():
    test_identifier, body = "s3cr3t", b'{"webhookEvent":"jira:issue_created"}'
    assert triggers._verify_signature(test_identifier, body, _sign(test_identifier, body)) is True


def test_verify_signature_accepts_prefixed_form():
    test_identifier, body = "s3cr3t", b"payload"
    provided = f"sha256={_sign(test_identifier, body)}"
    assert triggers._verify_signature(test_identifier, body, provided) is True


def test_verify_signature_rejects_wrong_secret():
    body = b"payload"
    assert triggers._verify_signature("right", body, _sign("wrong", body)) is False


def test_verify_signature_rejects_tampered_body():
    test_identifier = "s3cr3t"
    sig = _sign(test_identifier, b"original")
    assert triggers._verify_signature(test_identifier, b"tampered", sig) is False


def test_verify_signature_rejects_empty():
    assert triggers._verify_signature("", b"x", "abc") is False
    assert triggers._verify_signature("s", b"x", "") is False


# ── _event_matches ────────────────────────────────────────────────────────

def test_event_match_jira_positive():
    sched = {"event_source": "jira", "event_type": "issue_created"}
    payload = {"webhookEvent": "jira:issue_created"}
    assert triggers._event_matches(sched, payload, {}) is True


def test_event_match_jira_negative_dropped():
    sched = {"event_source": "jira", "event_type": "issue_created"}
    payload = {"webhookEvent": "jira:issue_deleted"}
    assert triggers._event_matches(sched, payload, {}) is False


def test_event_match_gitlab_header():
    sched = {"event_source": "gitlab", "event_type": "merge_request"}
    headers = {"X-Gitlab-Event": "Merge Request Hook"}
    assert triggers._event_matches(sched, {}, headers) is True


def test_event_match_no_filter_accepts_any():
    # No event_type configured → accept everything (plain webhook).
    assert triggers._event_matches({"event_source": "jira"}, {"webhookEvent": "x"}, {}) is True


# ── _rate_limited ─────────────────────────────────────────────────────────

def test_rate_limiter_allows_under_cap_then_blocks():
    tid = "trigger-rate-test-unique"
    triggers._webhook_hits.pop(tid, None)
    allowed = sum(0 if triggers._rate_limited(tid) else 1
                  for _ in range(triggers._WEBHOOK_RATE_MAX))
    assert allowed == triggers._WEBHOOK_RATE_MAX
    # next call over the cap is throttled
    assert triggers._rate_limited(tid) is True
    triggers._webhook_hits.pop(tid, None)
