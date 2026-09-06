# SPDX-License-Identifier: MIT
# ============================================================
# core/coach_events.py — channel resolution + emit-once invariant.
#
# Two things this pins down after the coverage fix:
#
#   1. channel_from_client_source() maps the free-form client_source
#      strings the gateway/CLI actually set onto the closed coach channel
#      vocabulary. In particular "platform" (the middleware default for
#      browser traffic) MUST resolve to "web" — several callers pass it
#      verbatim and the verification SQL groups on channel.
#
#   2. emit_coach_event() publishes EXACTLY ONCE per call. Only one /ask
#      streaming branch runs per request, so a single emit must produce a
#      single Kafka publish (produce() called once) and — because it is a
#      no-op-safe fire-and-forget — never raise. This guards against a
#      future refactor accidentally double-emitting or re-introducing the
#      "zero rows" gap.
#
# We mock produce() and the direct-ingest spawn so no Kafka/DB is needed.
# ============================================================

from __future__ import annotations

import pytest

from core import coach_events as ce
from core.coach_events import channel_from_client_source


# ── channel resolution ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "src, expected",
    [
        ("platform", "web"),   # middleware default for browser traffic
        ("web", "web"),
        ("ui", "web"),
        ("chat", "web"),
        ("cli", "cli"),
        ("terminal", "cli"),
        ("api", "api"),
        ("gateway", "api"),
        ("ide-vscode", "ide"),
        ("vscode", "ide"),
        ("ide", "ide"),
        ("teams", "teams"),
        ("voice", "voice"),
        ("web:chat", "web"),   # compound source → prefix fallback
        ("", "web"),           # empty → default
        (None, "web"),         # None → default
        ("something-unmapped", "web"),  # unknown → default
    ],
)
def test_channel_from_client_source(src, expected):
    assert channel_from_client_source(src) == expected


# ── emit-once invariant ──────────────────────────────────────────────────────

@pytest.fixture
def _emit_harness(monkeypatch):
    """Force ENABLE_COACH on, stub produce() + direct-ingest, and record calls.

    Returns a dict with the recorded produce() calls and ingest spawn count.
    """
    calls = {"produce": [], "ingest_spawns": 0}

    def _fake_produce(topic, payload, key=None):
        calls["produce"].append({"topic": topic, "payload": payload, "key": key})
        return True  # simulate a successful Kafka publish

    def _fake_spawn(payload):
        calls["ingest_spawns"] += 1

    # emit_coach_event reads module-level ENABLE_COACH and lazily imports
    # `produce` from core.kafka_producer, so patch both surfaces.
    monkeypatch.setattr(ce, "ENABLE_COACH", True, raising=False)
    monkeypatch.setattr("core.kafka_producer.produce", _fake_produce, raising=False)
    monkeypatch.setattr(ce, "_spawn_direct_ingest", _fake_spawn, raising=False)
    return calls


def test_emit_publishes_exactly_once(_emit_harness, monkeypatch):
    # Direct-ingest OFF so a successful Kafka publish is the only sink.
    monkeypatch.setattr(ce, "COACH_DIRECT_INGEST", False, raising=False)

    ce.emit_coach_event(
        user_id="u1",
        channel="platform",
        model="gpt-5.4",
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.0012,
        latency_ms=1500,
        request_id="req-1",
    )

    assert len(_emit_harness["produce"]) == 1, "expected exactly one Kafka publish"
    call = _emit_harness["produce"][0]
    assert call["payload"]["channel"] == "web"        # platform normalised
    assert call["payload"]["user_id"] == "u1"
    assert call["payload"]["model"] == "gpt-5.4"
    assert call["payload"]["tokens_in"] == 10
    assert call["payload"]["tokens_out"] == 20
    assert call["key"] == "u1"                          # partition key = user_id
    # Kafka succeeded + direct-ingest off → no synchronous ingest.
    assert _emit_harness["ingest_spawns"] == 0


def test_emit_direct_ingest_when_kafka_fails(monkeypatch):
    """When produce() returns False (Kafka off/unreachable), the event must
    still be ingested directly so it is never silently dropped."""
    calls = {"ingest_spawns": 0}

    monkeypatch.setattr(ce, "ENABLE_COACH", True, raising=False)
    monkeypatch.setattr(ce, "COACH_DIRECT_INGEST", False, raising=False)
    monkeypatch.setattr(
        "core.kafka_producer.produce", lambda *a, **k: False, raising=False
    )
    monkeypatch.setattr(
        ce, "_spawn_direct_ingest",
        lambda payload: calls.__setitem__("ingest_spawns", calls["ingest_spawns"] + 1),
        raising=False,
    )

    ce.emit_coach_event(user_id="u2", channel="cli", model="claude-sonnet-4-6")

    assert calls["ingest_spawns"] == 1  # fell back to direct ingest


def test_emit_is_noop_when_disabled(monkeypatch):
    """ENABLE_COACH off → no publish, no ingest, no raise."""
    produced = []
    monkeypatch.setattr(ce, "ENABLE_COACH", False, raising=False)
    monkeypatch.setattr(
        "core.kafka_producer.produce",
        lambda *a, **k: produced.append(1) or True,
        raising=False,
    )

    ce.emit_coach_event(user_id="u3", channel="web", model="gpt-5.4")

    assert produced == []


def test_emit_never_raises_on_bad_input(monkeypatch):
    """emit_coach_event is strictly observational — a broken produce() must
    not propagate to the request path."""
    monkeypatch.setattr(ce, "ENABLE_COACH", True, raising=False)
    monkeypatch.setattr(ce, "COACH_DIRECT_INGEST", False, raising=False)

    def _boom(*a, **k):
        raise RuntimeError("kafka exploded")

    monkeypatch.setattr("core.kafka_producer.produce", _boom, raising=False)
    monkeypatch.setattr(ce, "_spawn_direct_ingest", lambda payload: None, raising=False)

    # Must not raise.
    ce.emit_coach_event(user_id="u4", channel="web", model="gpt-5.4")
