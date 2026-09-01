# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Regression test for issue 3: endpoints with their own dedicated rate
# limiter (AUTH_LOGIN, AUTH_REGISTER, AUTH_REFRESH, SSO_CALLBACK) must be
# exempt from the global GLOBAL_API_IP/GLOBAL_API_USER backstop inside
# RateLimitMiddleware.dispatch() -- otherwise unrelated traffic sharing an
# IP can trip the 300 req/60s global bucket and reject a login request
# that AUTH_LOGIN itself would still allow.
#
# Strategy: build a minimal Starlette app with the real RateLimitMiddleware
# mounted, force RATE_LIMIT_ENABLED=True and a tiny GLOBAL_API_IP limit
# (1 request), then confirm:
#   - a normal endpoint gets 429 on the 2nd request from the same IP.
#   - /auth/login is NEVER blocked by the global backstop, no matter how
#     many prior "global" requests came from the same IP.
# ============================================================

from __future__ import annotations

from dataclasses import replace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from middleware.rate_limit_middleware import RateLimitMiddleware
import core.rate_limiter as _rl


async def _ok(request):
    return JSONResponse({"ok": True})


def _build_app():
    app = Starlette(routes=[
        Route("/ainxt/v1/api/other/endpoint", _ok, methods=["GET"]),
        Route("/ainxt/v1/api/auth/login", _ok, methods=["POST"]),
    ])
    app.add_middleware(RateLimitMiddleware)
    return app


@pytest.fixture
def tiny_global_ip_limit(monkeypatch):
    """Force rate limiting on with a 1-request/60s global IP bucket, and
    route Redis lookups to an isolated fallback dict so this test never
    touches real Redis state."""
    monkeypatch.setattr(_rl, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(_rl, "_get_redis", lambda: None)  # force in-process fallback counter
    monkeypatch.setattr(_rl, "_fallback_counters", {})    # isolated counter state per test

    tiny_ip_cfg = replace(_rl.GLOBAL_API_IP, limit=1)
    monkeypatch.setattr(_rl, "GLOBAL_API_IP", tiny_ip_cfg)

    # middleware/rate_limit_middleware.py imports GLOBAL_API_IP fresh from
    # core.rate_limiter inside dispatch(), so patching the module attribute
    # above is sufficient -- no separate patch needed there.
    yield


def test_normal_endpoint_is_blocked_after_global_ip_limit(tiny_global_ip_limit):
    client = TestClient(_build_app())

    r1 = client.get("/ainxt/v1/api/other/endpoint")
    assert r1.status_code == 200

    r2 = client.get("/ainxt/v1/api/other/endpoint")
    assert r2.status_code == 429, "second request from the same IP should trip the 1-req/60s global bucket"


def test_login_endpoint_is_exempt_from_global_ip_backstop(tiny_global_ip_limit):
    client = TestClient(_build_app())

    # Exhaust the global IP bucket via the unrelated endpoint first.
    client.get("/ainxt/v1/api/other/endpoint")
    r_blocked = client.get("/ainxt/v1/api/other/endpoint")
    assert r_blocked.status_code == 429

    # /auth/login must still succeed -- it is exempt from GLOBAL_API_IP,
    # even though the same IP has already exceeded that bucket.
    r_login = client.post("/ainxt/v1/api/auth/login")
    assert r_login.status_code == 200, (
        "login should be exempt from the global IP backstop even after "
        "the IP has exhausted GLOBAL_API_IP via unrelated traffic"
    )
