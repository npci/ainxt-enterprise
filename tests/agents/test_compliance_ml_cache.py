# SPDX-License-Identifier: Apache-2.0
# ============================================================
# ComplianceEngine — ML result cache (B6) unit tests
# ============================================================
#
# The browser-agent passthrough lane skips history compression but keeps
# tool-result scanning, so byte-identical prior-round content would otherwise
# be re-scanned by privacy_svc every turn (O(N^2) blocking HTTP calls). B6 adds
# a content-hash LRU that memoizes the deterministic ML verdict.
#
# These tests exercise the pure cache helpers (_ml_cache_key/_get/_put) and the
# _call_privacy_svc integration with a MOCKED http client — no live privacy_svc
# is required. The critical safety invariant is verified: only clean 200
# responses are cached; non-200 and exception paths are never memoized.
# ============================================================

import pytest

import agents.compliance_engine as ce_mod
from agents.compliance_engine import (
    _ml_cache_key,
    _ml_cache_get,
    _ml_cache_put,
    compliance_engine,
)


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty cache and cache enabled."""
    with ce_mod._ml_cache_lock:
        ce_mod._ml_cache.clear()
    yield
    with ce_mod._ml_cache_lock:
        ce_mod._ml_cache.clear()


class _FakeResp:
    def __init__(self, status_code=200, entities=None):
        self.status_code = status_code
        self._entities = entities if entities is not None else []

    def json(self):
        return {"results": [self._entities]}


class _FakeHTTP:
    """Records how many times /filter was actually hit."""

    def __init__(self, resp):
        self._resp = resp
        self.calls = 0

    def post(self, *a, **kw):
        self.calls += 1
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


# ── pure cache-helper tests ────────────────────────────────────────────────

def test_cache_key_is_deterministic_sha256():
    assert _ml_cache_key("hello") == _ml_cache_key("hello")
    assert _ml_cache_key("hello") != _ml_cache_key("world")
    # 64 hex chars = sha256
    assert len(_ml_cache_key("x")) == 64


def test_cache_miss_returns_none():
    assert _ml_cache_get(_ml_cache_key("never-stored")) is None


def test_put_then_get_roundtrip():
    key = _ml_cache_key("some text")
    findings = [{"type": "CARD", "value": "x", "blocked": True}]
    _ml_cache_put(key, findings)
    got = _ml_cache_get(key)
    assert got == findings


def test_get_returns_defensive_copy():
    """Callers must not be able to mutate the cached entry."""
    key = _ml_cache_key("abc")
    _ml_cache_put(key, [{"type": "PAN", "blocked": True}])
    got = _ml_cache_get(key)
    got[0]["blocked"] = False       # mutate the returned copy
    got.append({"type": "EVIL"})
    # cache is unaffected
    fresh = _ml_cache_get(key)
    assert len(fresh) == 1
    assert fresh[0]["blocked"] is True


def test_put_stores_defensive_copy():
    """Mutating the source list after put must not affect the cached entry."""
    key = _ml_cache_key("def")
    src = [{"type": "AADHAAR", "blocked": True}]
    _ml_cache_put(key, src)
    src[0]["blocked"] = False
    src.append({"type": "X"})
    fresh = _ml_cache_get(key)
    assert len(fresh) == 1
    assert fresh[0]["blocked"] is True


def test_lru_eviction_respects_max_size(monkeypatch):
    monkeypatch.setattr(ce_mod, "_ML_CACHE_MAX", 3)
    for i in range(5):
        _ml_cache_put(_ml_cache_key(f"k{i}"), [{"type": f"T{i}"}])
    with ce_mod._ml_cache_lock:
        assert len(ce_mod._ml_cache) == 3
    # oldest two (k0, k1) evicted; newest three remain
    assert _ml_cache_get(_ml_cache_key("k0")) is None
    assert _ml_cache_get(_ml_cache_key("k1")) is None
    assert _ml_cache_get(_ml_cache_key("k4")) is not None


def test_lru_get_refreshes_recency(monkeypatch):
    monkeypatch.setattr(ce_mod, "_ML_CACHE_MAX", 3)
    for i in range(3):
        _ml_cache_put(_ml_cache_key(f"k{i}"), [{"type": f"T{i}"}])
    # touch k0 so it becomes most-recent
    assert _ml_cache_get(_ml_cache_key("k0")) is not None
    # insert a 4th → k1 (now oldest) should be evicted, not k0
    _ml_cache_put(_ml_cache_key("k3"), [{"type": "T3"}])
    assert _ml_cache_get(_ml_cache_key("k0")) is not None
    assert _ml_cache_get(_ml_cache_key("k1")) is None


# ── _call_privacy_svc integration (mocked HTTP) ─────────────────────────────

def test_second_call_served_from_cache(monkeypatch):
    """Identical text hits privacy_svc once, then is served from cache."""
    monkeypatch.setattr(ce_mod, "_ML_CACHE_ENABLED", True)
    fake = _FakeHTTP(_FakeResp(200, entities=[]))
    monkeypatch.setattr(ce_mod, "_http_client", fake)

    text = "the quick brown fox"
    r1 = compliance_engine._call_privacy_svc(text)
    r2 = compliance_engine._call_privacy_svc(text)

    assert r1 == r2
    assert fake.calls == 1, "second identical call must be served from cache"
    # metrics reflect the skipped call
    assert ce_mod._tl.ml_called is False
    assert ce_mod._tl.privacy_svc_latency_ms == 0.0


def test_distinct_text_not_shared(monkeypatch):
    monkeypatch.setattr(ce_mod, "_ML_CACHE_ENABLED", True)
    fake = _FakeHTTP(_FakeResp(200, entities=[]))
    monkeypatch.setattr(ce_mod, "_http_client", fake)

    compliance_engine._call_privacy_svc("text one")
    compliance_engine._call_privacy_svc("text two")
    assert fake.calls == 2


def test_non_200_is_not_cached(monkeypatch):
    """A transient failure must not be memoized as 'no findings'."""
    monkeypatch.setattr(ce_mod, "_ML_CACHE_ENABLED", True)
    fake = _FakeHTTP(_FakeResp(503))
    monkeypatch.setattr(ce_mod, "_http_client", fake)

    text = "sensitive content"
    compliance_engine._call_privacy_svc(text)
    compliance_engine._call_privacy_svc(text)
    # both turns retried the service — nothing cached
    assert fake.calls == 2
    assert _ml_cache_get(_ml_cache_key(text)) is None


def test_exception_is_not_cached(monkeypatch):
    monkeypatch.setattr(ce_mod, "_ML_CACHE_ENABLED", True)
    fake = _FakeHTTP(RuntimeError("connection reset"))
    monkeypatch.setattr(ce_mod, "_http_client", fake)

    text = "boom"
    out = compliance_engine._call_privacy_svc(text)
    assert out == []                      # never raises
    compliance_engine._call_privacy_svc(text)
    assert fake.calls == 2                # retried, not cached
    assert _ml_cache_get(_ml_cache_key(text)) is None


def test_disabled_cache_always_calls_service(monkeypatch):
    monkeypatch.setattr(ce_mod, "_ML_CACHE_ENABLED", False)
    fake = _FakeHTTP(_FakeResp(200, entities=[]))
    monkeypatch.setattr(ce_mod, "_http_client", fake)

    text = "repeated text"
    compliance_engine._call_privacy_svc(text)
    compliance_engine._call_privacy_svc(text)
    assert fake.calls == 2, "cache disabled → every call hits the service"


def test_cached_findings_are_byte_identical(monkeypatch):
    """Cache hit returns the same verdict as the live 200 response."""
    monkeypatch.setattr(ce_mod, "_ML_CACHE_ENABLED", True)
    # "account_number" maps to a real compliance type via _ML_LABEL_MAP.
    entities = [{"entity_group": "account_number", "word": "123456789012", "score": 0.97}]
    fake = _FakeHTTP(_FakeResp(200, entities=entities))
    monkeypatch.setattr(ce_mod, "_http_client", fake)

    text = "account 123456789012"
    live = compliance_engine._call_privacy_svc(text)
    cached = compliance_engine._call_privacy_svc(text)
    assert live == cached
    assert live and live[0]["type"] == "ACCOUNT_NUMBER"
    assert fake.calls == 1
