# SPDX-License-Identifier: MIT
"""Tests for M365 tool-visibility gating in AB Studio /tools-catalog.

Microsoft 365 tools must only appear when the requesting user has an active
M365 connection. Jira/GitLab/other tools are always shown; the connection
check is fail-safe (hidden on error).

We call the endpoint coroutine directly with monkeypatched dependencies
(workflow_repo.list_tools + is_m365_connected) so we don't boot the whole
AB Studio app.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ABS_BACKEND = Path(__file__).resolve().parents[2] / "ABStudio" / "backend"
if str(_ABS_BACKEND) not in sys.path:
    sys.path.insert(0, str(_ABS_BACKEND))

import app.api.catalog as catalog  # noqa: E402
from app.models import AuthenticatedUser  # noqa: E402


_ROWS = [
    {"name": "outlook_send_mail", "service": "microsoft_365", "description": "", "input_schema": {}},
    {"name": "calendar_list_events", "service": "microsoft_365", "description": "", "input_schema": {}},
    {"name": "jira_create_issue", "service": "jira", "description": "", "input_schema": {}},
    {"name": "gitlab_read_file", "service": "gitlab", "description": "", "input_schema": {}},
]


def _user(uid="u-real"):
    return AuthenticatedUser(id=uid, email="x@y.com", full_name="X", role="user")


async def _fake_list_tools():
    return list(_ROWS)


@pytest.mark.asyncio
async def test_m365_hidden_when_not_connected(monkeypatch):
    monkeypatch.setattr(catalog.workflow_repo, "list_tools", _fake_list_tools)

    async def _not_connected(_uid):
        return False

    monkeypatch.setattr(catalog, "is_m365_connected", _not_connected, raising=False)
    # is_m365_connected is imported inside the handler; patch the source module too.
    import app.core.m365_connection as mc
    monkeypatch.setattr(mc, "is_m365_connected", _not_connected)

    result = await catalog.list_tools_catalog(current_user=_user())
    names = {t["name"] for t in result["tools"]}
    assert "outlook_send_mail" not in names
    assert "calendar_list_events" not in names
    # Non-M365 tools unaffected.
    assert "jira_create_issue" in names
    assert "gitlab_read_file" in names


@pytest.mark.asyncio
async def test_m365_shown_when_connected(monkeypatch):
    monkeypatch.setattr(catalog.workflow_repo, "list_tools", _fake_list_tools)

    async def _connected(_uid):
        return True

    import app.core.m365_connection as mc
    monkeypatch.setattr(mc, "is_m365_connected", _connected)

    result = await catalog.list_tools_catalog(current_user=_user())
    names = {t["name"] for t in result["tools"]}
    assert "outlook_send_mail" in names
    assert "calendar_list_events" in names
    assert "jira_create_issue" in names
