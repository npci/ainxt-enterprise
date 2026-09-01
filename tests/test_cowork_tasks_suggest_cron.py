# SPDX-License-Identifier: Apache-2.0
"""Contract tests for POST /buddy/tasks/suggest-cron.

The endpoint converts a natural-language schedule description into a
validated 5-field cron expression via the LLM (models.model_router).
It is a pure inference endpoint — no DB writes, no side effects — so the
tests only need to patch the LLM call and the auth dependency.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.cowork_tasks_router as ct
from auth.dependencies import get_current_user


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _fake_user():
    return {"sub": "user-abc", "email": "testuser@example.com"}


def _make_client(monkeypatch, llm_response: str | Exception):
    """Wire up a fresh app that mounts the router, with the LLM patched to
    return `llm_response` (a string) or raise it (an Exception instance)."""
    # Bypass auth — the endpoint just needs current_user["sub"]/["email"].
    app = FastAPI()
    app.include_router(ct.router)
    app.dependency_overrides[get_current_user] = _fake_user

    class _FakeModelRouter:
        def generate(self, prompt, model_hint=None):  # noqa: ARG002 — signature match
            if isinstance(llm_response, Exception):
                raise llm_response
            return llm_response

    # The endpoint does `from models.model_router import model_router` at call
    # time, so patch the attribute on that module.
    import models.model_router as mr
    monkeypatch.setattr(mr, "model_router", _FakeModelRouter())

    return TestClient(app)


# ── Happy paths ──────────────────────────────────────────────────────────────

def test_suggest_cron_returns_llm_answer(monkeypatch):
    """The LLM returns clean JSON; the endpoint echoes it after cron validation."""
    llm = '{"cron": "0 9 * * 1-5", "summary": "Every weekday at 09:00"}'
    client = _make_client(monkeypatch, llm)

    r = client.post("/buddy/tasks/suggest-cron", json={"description": "every weekday at 9am"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cron"] == "0 9 * * 1-5"
    assert body["summary"] == "Every weekday at 09:00"
    assert body["tz"] == "UTC"


def test_suggest_cron_extracts_cron_from_prose_fallback(monkeypatch):
    """If the LLM wraps its answer in prose, the regex fallback still salvages
    the 5-field cron token."""
    llm = "Sure — here is the schedule: 30 8 * * * — it runs daily at 08:30."
    client = _make_client(monkeypatch, llm)

    r = client.post("/buddy/tasks/suggest-cron", json={"description": "daily 8:30 am"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cron"] == "30 8 * * *"
    # No structured summary in the prose fallback — endpoint synthesises one.
    assert "30 8 * * *" in body["summary"] or body["summary"]


def test_suggest_cron_strips_markdown_fences(monkeypatch):
    """Some models wrap JSON in ```json fences; the parser tolerates that."""
    llm = '```json\n{"cron": "0 0 1 * *", "summary": "Monthly on the 1st at midnight"}\n```'
    client = _make_client(monkeypatch, llm)

    r = client.post("/buddy/tasks/suggest-cron", json={"description": "first of every month at midnight"})
    assert r.status_code == 200, r.text
    assert r.json()["cron"] == "0 0 1 * *"


# ── Validation / error paths ─────────────────────────────────────────────────

def test_suggest_cron_rejects_invalid_llm_output(monkeypatch):
    """When the LLM returns nothing cron-shaped, the endpoint returns 422 with
    a UI-friendly message rather than an obscure parse error."""
    client = _make_client(monkeypatch, "sorry, I have no idea")

    r = client.post("/buddy/tasks/suggest-cron", json={"description": "hey"})
    assert r.status_code == 422
    assert "Daily" in r.json()["detail"] or "Weekly" in r.json()["detail"]


def test_suggest_cron_rejects_bogus_cron_token(monkeypatch):
    """A 5-token string that isn't a real cron (e.g. impossible day) must
    still be rejected by croniter validation."""
    # 60 minutes is out of range — croniter should reject it.
    llm = '{"cron": "60 9 * * *", "summary": "bad"}'
    client = _make_client(monkeypatch, llm)

    r = client.post("/buddy/tasks/suggest-cron", json={"description": "impossible"})
    assert r.status_code == 422


def test_suggest_cron_returns_503_on_llm_failure(monkeypatch):
    """If the LLM call itself blows up, the endpoint surfaces a 503 —
    calling code should retry or fall back to the friendly picker."""
    client = _make_client(monkeypatch, RuntimeError("gateway down"))

    r = client.post("/buddy/tasks/suggest-cron", json={"description": "every day at 9"})
    assert r.status_code == 503


def test_suggest_cron_rejects_empty_description(monkeypatch):
    """Pydantic min_length=1 refuses an empty description before we reach the LLM."""
    client = _make_client(monkeypatch, "unused")

    r = client.post("/buddy/tasks/suggest-cron", json={"description": ""})
    # Pydantic v1 -> 422, v2 -> 422. Empty is definitely rejected.
    assert r.status_code == 422


def test_suggest_cron_rejects_oversize_description(monkeypatch):
    """Descriptions above 500 chars are rejected before the LLM is called."""
    client = _make_client(monkeypatch, "unused")

    r = client.post("/buddy/tasks/suggest-cron", json={"description": "x" * 501})
    assert r.status_code == 422
