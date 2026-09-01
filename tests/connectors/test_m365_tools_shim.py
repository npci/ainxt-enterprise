# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the AB Studio M365 tool shim (m365_tools.py).

The shim is a self-contained code string executed in the AB Studio sandbox.
Here we exec each tool's ``code`` in a fresh namespace with a fake urllib
opener so we can assert:
  - the request body sent to /connectors/execute (connector/tool/params/user_id)
  - the X-Bridge-Token header
  - success passthrough (items+count for reads, success for writes)
  - error mapping (reauth/scope), missing user context, missing bridge config
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

# The module lives under ABStudio/backend/app/tools; add that root to sys.path
# so ``from app.tools.m365_tools import M365_TOOLS`` resolves without booting
# the whole AB Studio app.
_ABS_BACKEND = Path(__file__).resolve().parents[2] / "ABStudio" / "backend"
if str(_ABS_BACKEND) not in sys.path:
    sys.path.insert(0, str(_ABS_BACKEND))

from app.tools.m365_tools import M365_TOOLS  # noqa: E402


def _tool(name: str) -> dict:
    for spec in M365_TOOLS:
        if spec["name"] == name:
            return spec
    raise AssertionError(f"tool {name!r} not in M365_TOOLS")


class _FakeHTTPError(Exception):
    def __init__(self, code, body=b""):
        self.code = code
        self._body = body

    def read(self):
        return self._body


class _FakeURLError(Exception):
    def __init__(self, reason):
        self.reason = reason


def _build_fake_urllib(response=None, raise_exc=None, capture=None):
    """Return a fake ``urllib`` package exposing .request/.parse/.error."""
    import types

    request_mod = types.ModuleType("urllib.request")
    parse_mod = types.ModuleType("urllib.parse")
    error_mod = types.ModuleType("urllib.error")

    error_mod.HTTPError = _FakeHTTPError
    error_mod.URLError = _FakeURLError

    class _Req:
        def __init__(self, url, data=None, headers=None, method=None):
            self.url = url
            self.data = data
            self.headers = headers or {}
            self.method = method
            if capture is not None:
                capture["url"] = url
                capture["data"] = data
                capture["headers"] = headers
                capture["method"] = method

    class _Resp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._payload

    class _Opener:
        def open(self, req, timeout=None):
            if raise_exc is not None:
                raise raise_exc
            return _Resp(response or {})

    request_mod.Request = _Req
    request_mod.HTTPSHandler = lambda **kw: object()
    request_mod.ProxyHandler = lambda *a, **kw: object()
    request_mod.build_opener = lambda *a, **kw: _Opener()

    urllib_pkg = types.ModuleType("urllib")
    urllib_pkg.request = request_mod
    urllib_pkg.parse = parse_mod
    urllib_pkg.error = error_mod
    return urllib_pkg, request_mod, parse_mod, error_mod


def _run_tool(name, inputs, env, response=None, raise_exc=None, capture=None):
    """Exec a tool's code in an isolated namespace and call run(inputs)."""
    spec = _tool(name)
    urllib_pkg, request_mod, parse_mod, error_mod = _build_fake_urllib(
        response=response, raise_exc=raise_exc, capture=capture,
    )

    import os as _os_real

    class _FakeEnviron(dict):
        pass

    fake_os = type(sys)("os")
    fake_os.environ = _FakeEnviron(env)
    # Passthrough for anything else the shim might touch (it only uses environ).
    fake_os.getenv = lambda k, d=None: env.get(k, d)

    ns = {
        "__builtins__": __builtins__,
        "os": fake_os,
        "json": json,
        "urllib": urllib_pkg,
        # ``import urllib.request`` inside the code binds these names too:
        "io": io,
    }
    # The code does ``import os, json, urllib.request, ...`` — provide those in
    # sys.modules so the import statements resolve to our fakes.
    saved = {}
    for modname, mod in {
        "os": fake_os,
        "urllib": urllib_pkg,
        "urllib.request": request_mod,
        "urllib.parse": parse_mod,
        "urllib.error": error_mod,
    }.items():
        saved[modname] = sys.modules.get(modname)
        sys.modules[modname] = mod
    try:
        exec(spec["code"], ns)
        return ns["run"](inputs)
    finally:
        for modname, mod in saved.items():
            if mod is None:
                sys.modules.pop(modname, None)
            else:
                sys.modules[modname] = mod


_ENV = {
    "PLATFORM_BASE_URL": "https://plat.example",
    "AZURE_AD_CLIENT_SECRET": "secret-token",
    "AINXT_USER_ID": "user-42",
}


# ── catalog sanity ──────────────────────────────────────────────────────────

def test_nine_phase1_tools_present():
    names = {t["name"] for t in M365_TOOLS}
    assert names == {
        "outlook_search_emails", "outlook_read_email", "outlook_send_mail",
        "teams_send_message", "teams_start_chat", "teams_get_chat_messages",
        "calendar_list_events", "calendar_create_event", "people_search",
    }
    for t in M365_TOOLS:
        assert t["service"] == "microsoft_365"
        assert "draft" not in t  # all active in Phase 1
        assert "def run" in t["code"]


# ── request construction ────────────────────────────────────────────────────

def test_read_builds_correct_request_body():
    capture = {}
    result = _run_tool(
        "outlook_search_emails",
        {"search_query": "budget"},
        _ENV,
        response={"success": True, "items": [{"id": "m1"}], "count": 1},
        capture=capture,
    )
    assert result == {"success": True, "items": [{"id": "m1"}], "count": 1}

    assert capture["url"].endswith("/connectors/execute")
    assert capture["headers"]["X-Bridge-Token"] == "secret-token"
    sent = json.loads(capture["data"].decode())
    assert sent == {
        "connector": "microsoft_365",
        "tool": "outlook_search_emails",
        "params": {"search_query": "budget"},
        "user_id": "user-42",
    }


def test_write_returns_success():
    result = _run_tool(
        "outlook_send_mail",
        {"to": "a@b.com", "subject": "Hi", "body": "hello"},
        _ENV,
        response={"success": True},
    )
    assert result == {"success": True}


# ── error mapping ───────────────────────────────────────────────────────────

def test_success_false_surfaces_error_message():
    result = _run_tool(
        "outlook_search_emails", {}, _ENV,
        response={"success": False, "error": "please reconnect", "code": "REAUTH_REQUIRED"},
    )
    assert result == {"error": "please reconnect"}


def test_http_422_maps_to_compliance_message():
    result = _run_tool(
        "outlook_send_mail",
        {"to": "a@b.com", "subject": "s", "body": "b"},
        _ENV,
        raise_exc=_FakeHTTPError(422, b"blocked: PAN"),
    )
    assert "compliance" in result["error"].lower()


def test_url_error_maps_to_unreachable():
    result = _run_tool(
        "people_search", {"search_query": "x"}, _ENV,
        raise_exc=_FakeURLError("connection refused"),
    )
    assert "unreachable" in result["error"].lower()


# ── missing context / config ────────────────────────────────────────────────

def test_missing_user_context():
    env = dict(_ENV)
    env["AINXT_USER_ID"] = ""
    result = _run_tool("outlook_search_emails", {}, env, response={"success": True})
    assert "no user context" in result["error"].lower()


def test_missing_bridge_token():
    """Empty AZURE_AD_CLIENT_SECRET triggers the not-configured guard."""
    env = {
        "PLATFORM_BASE_URL": "https://plat.example",
        "AZURE_AD_CLIENT_SECRET": "",
        "AINXT_USER_ID": "user-42",
    }
    result = _run_tool("outlook_search_emails", {}, env, response={"success": True})
    assert "not configured" in result["error"].lower()


def test_loopback_default_when_platform_base_url_missing():
    """No PLATFORM_BASE_URL → shim uses the loopback default so same-host
    deploys work without any URL var. Token is still required."""
    env = {
        "PLATFORM_BASE_URL": "",
        "AZURE_AD_CLIENT_SECRET": "shared-secret",
        "AINXT_USER_ID": "user-99",
    }
    capture = {}
    result = _run_tool(
        "people_search", {"search_query": "x"}, env,
        response={"success": True, "items": [], "count": 0},
        capture=capture,
    )
    assert result["success"] is True
    assert capture["url"] == "http://127.0.0.1:8000/ainxt/v1/api/connectors/execute"
    assert capture["headers"]["X-Bridge-Token"] == "shared-secret"
