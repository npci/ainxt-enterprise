# SPDX-License-Identifier: MIT
# ============================================================
# REQUEST ID MIDDLEWARE
#
# Ensures every request carries a stable request_id from the
# moment it enters the gateway, regardless of which router
# handles it.
#
# Behaviour
# ---------
#  1. Reads x-client-request-id (preferred — set by CLI/IDE/browser)
#     or x-request-id (generic fallback) from the incoming headers.
#  2. Falls back to a fresh UUID when neither header is present.
#  3. Binds the ID to the thread-local logger context via
#     set_request_id() + set_correlation_id() so every log line
#     emitted by any downstream code (routers, gateways, agents)
#     automatically carries the correct request_id.
#  4. Stores the ID in request.state.request_id for handlers that
#     need it as a plain string.
#  5. Echoes the resolved ID back in the X-Request-ID response
#     header so clients can correlate their own logs.
#  6. Clears the bound context after the response so reused
#     worker threads never carry a stale ID into the next request.
#
# Registration
# ------------
# Added in gateway.py BEFORE BudgetMiddleware so the ID is
# available to every subsequent middleware and handler.
# ============================================================

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logger import (
    set_request_id,
    set_correlation_id,
    clear_bound_context,
)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Propagate a stable request_id through the entire request lifecycle.

    Priority order for the incoming ID:
      1. x-client-request-id  (set by ainxt-cli, IDE plugins, browser UI)
      2. x-request-id         (generic HTTP correlation header)
      3. Fresh UUID4           (generated here when neither header is present)
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = (
            (request.headers.get("x-client-request-id") or "").strip()
            or (request.headers.get("x-request-id") or "").strip()
            or str(uuid.uuid4())
        )

        # Bind to thread-local logger context — all downstream log lines
        # will automatically include request_id and correlation_id.
        set_request_id(rid)
        # Unconditionally overwrite (not bind_context) so a reused worker
        # thread never inherits a stale correlation_id from a prior request.
        set_correlation_id(rid)

        # Make available to handlers as a plain attribute.
        request.state.request_id = rid

        response = await call_next(request)

        # Echo back so clients and Grafana/Loki can correlate.
        response.headers["X-Request-ID"] = rid

        # Clear bound context — thread-locals persist across requests on
        # reused Gunicorn/uvicorn worker threads; clean up to prevent bleed.
        clear_bound_context()

        return response
