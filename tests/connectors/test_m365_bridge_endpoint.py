# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the internal M365 connector bridge.

Covers POST /connectors/execute (routers/connectors_router.py):
  - rejects a missing/bad bridge token (401)
  - returns items for a read tool (full ConnectorResponse.to_dict passthrough)
  - returns a success ack for a write tool
  - passes the explicit user_id through to ConnectorEngine.execute
  - maps REAUTH_REQUIRED to a structured {success:false, code} at HTTP 200

Bridge auth reuses the ``AZURE_AD_CLIENT_SECRET`` env var as the shared
service-to-service secret (see ``_bridge_token_ok`` in the router for the
rotation trade-offs).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.connectors_router as cr
from connectors.base import ConnectorResponse


_TOKEN = "test-bridge-secret"


class _FakeRegistry:
    """Stand-in for connector_registry.execute that records the call."""

    def __init__(self, response: ConnectorResponse):
        self._response = response
        self.calls: list[tuple] = []

    def execute(self, connector, tool, params, user_id, query_text="", call_counter=None):
        self.calls.append((connector, tool, params, user_id, query_text, call_counter))
        return self._response


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AZURE_AD_CLIENT_SECRET", _TOKEN)
    app = FastAPI()
    app.include_router(cr.router)
    return TestClient(app)


def _mk_client_with_registry(monkeypatch, response: ConnectorResponse) -> tuple[TestClient, _FakeRegistry]:
    monkeypatch.setenv("AZURE_AD_CLIENT_SECRET", _TOKEN)
    fake = _FakeRegistry(response)
    monkeypatch.setattr(cr, "connector_registry", fake)
    app = FastAPI()
    app.include_router(cr.router)
    return TestClient(app), fake


def _body(tool="outlook_search_emails", params=None, user_id="user-123"):
    return {
        "connector": "microsoft_365",
        "tool": tool,
        "params": params or {},
        "user_id": user_id,
    }


# ── auth ───────────────────────────────────────────────────────────────────

def test_missing_token_rejected(client):
    resp = client.post("/connectors/execute", json=_body())
    assert resp.status_code == 401


def test_wrong_token_rejected(client):
    resp = client.post(
        "/connectors/execute",
        json=_body(),
        headers={"X-Bridge-Token": "nope"},
    )
    assert resp.status_code == 401


def test_no_token_configured_fails_closed(monkeypatch):
    # Empty configured token disables the endpoint entirely.
    monkeypatch.setenv("AZURE_AD_CLIENT_SECRET", "")
    app = FastAPI()
    app.include_router(cr.router)
    c = TestClient(app)
    resp = c.post("/connectors/execute", json=_body(), headers={"X-Bridge-Token": ""})
    assert resp.status_code == 401


# ── reads / writes ─────────────────────────────────────────────────────────

def test_read_returns_items_and_passes_user_id(monkeypatch):
    response = ConnectorResponse(
        success=True,
        items=[{"id": "m1", "subject": "Hi"}],
        count=1,
        source="microsoft_365",
        tool="outlook_search_emails",
    )
    c, fake = _mk_client_with_registry(monkeypatch, response)

    resp = c.post(
        "/connectors/execute",
        json=_body(params={"search_query": "budget"}, user_id="user-xyz"),
        headers={"X-Bridge-Token": _TOKEN},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["items"] == [{"id": "m1", "subject": "Hi"}]
    assert data["count"] == 1

    # user_id from the body reached the engine unchanged.
    assert fake.calls, "engine.execute was not called"
    connector, tool, params, user_id, _q, _cc = fake.calls[0]
    assert connector == "microsoft_365"
    assert tool == "outlook_search_emails"
    assert user_id == "user-xyz"
    assert params.get("search_query") == "budget"


def test_write_returns_success_ack(monkeypatch):
    response = ConnectorResponse(
        success=True, items=[], count=0,
        source="microsoft_365", tool="outlook_send_mail",
    )
    c, _fake = _mk_client_with_registry(monkeypatch, response)
    resp = c.post(
        "/connectors/execute",
        json=_body(
            tool="outlook_send_mail",
            params={"to": "a@b.com", "subject": "Hi", "body": "hello there"},
        ),
        headers={"X-Bridge-Token": _TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_reauth_mapped_to_structured_error(monkeypatch):
    response = ConnectorResponse(
        success=False, items=[], count=0,
        source="microsoft_365", tool="outlook_search_emails",
        error="REAUTH_REQUIRED: token expired",
    )
    c, _fake = _mk_client_with_registry(monkeypatch, response)
    resp = c.post(
        "/connectors/execute",
        json=_body(),
        headers={"X-Bridge-Token": _TOKEN},
    )
    # HTTP 200 with structured payload so the sandbox can relay cleanly.
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["code"] == "REAUTH_REQUIRED"
    assert "reconnect" in data["error"].lower()


def test_empty_user_id_returns_no_user(client):
    resp = client.post(
        "/connectors/execute",
        json=_body(user_id=""),
        headers={"X-Bridge-Token": _TOKEN},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["code"] == "NO_USER"


# ── /connectors/status-for-user (tool-visibility gate) ──────────────────────

class _FakeStatusRegistry:
    def __init__(self, statuses):
        self._statuses = statuses
        self.calls: list[str] = []

    def get_user_status(self, user_id):
        self.calls.append(user_id)
        return self._statuses


def _mk_status_client(monkeypatch, statuses):
    monkeypatch.setenv("AZURE_AD_CLIENT_SECRET", _TOKEN)
    fake = _FakeStatusRegistry(statuses)
    monkeypatch.setattr(cr, "connector_registry", fake)
    app = FastAPI()
    app.include_router(cr.router)
    return TestClient(app), fake


def test_status_missing_token_rejected(client):
    resp = client.post("/connectors/status-for-user", json={"user_id": "u1"})
    assert resp.status_code == 401


def test_status_connected_true(monkeypatch):
    c, fake = _mk_status_client(monkeypatch, [
        {"name": "microsoft_365", "connected": True},
        {"name": "jira", "connected": False},
    ])
    resp = c.post(
        "/connectors/status-for-user",
        json={"user_id": "u-1", "connector": "microsoft_365"},
        headers={"X-Bridge-Token": _TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == {"connected": True, "connector": "microsoft_365"}
    assert fake.calls == ["u-1"]


def test_status_not_connected_false(monkeypatch):
    c, _fake = _mk_status_client(monkeypatch, [
        {"name": "microsoft_365", "connected": False},
    ])
    resp = c.post(
        "/connectors/status-for-user",
        json={"user_id": "u-1"},
        headers={"X-Bridge-Token": _TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json()["connected"] is False


def test_status_empty_user_false(monkeypatch):
    c, _fake = _mk_status_client(monkeypatch, [{"name": "microsoft_365", "connected": True}])
    resp = c.post(
        "/connectors/status-for-user",
        json={"user_id": ""},
        headers={"X-Bridge-Token": _TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json()["connected"] is False


def test_status_lookup_error_fails_safe(monkeypatch):
    class _Boom:
        def get_user_status(self, user_id):
            raise RuntimeError("db down")

    monkeypatch.setenv("AZURE_AD_CLIENT_SECRET", _TOKEN)
    monkeypatch.setattr(cr, "connector_registry", _Boom())
    app = FastAPI()
    app.include_router(cr.router)
    c = TestClient(app)
    resp = c.post(
        "/connectors/status-for-user",
        json={"user_id": "u-1"},
        headers={"X-Bridge-Token": _TOKEN},
    )
    # Fail-safe: error → connected:false, still HTTP 200.
    assert resp.status_code == 200
    assert resp.json()["connected"] is False
