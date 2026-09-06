# SPDX-License-Identifier: MIT
# ============================================================
# CLIENT SOURCE MIDDLEWARE
#
# Detects which client is making the request and tags every
# request with a canonical client_source value:
#
#   platform      — React web UI (browser)
#   cli           — ainxt-cli (terminal)
#   ide-vscode    — VS Code extension
#   ide-jetbrains — JetBrains (PyCharm / IntelliJ) plugin
#   api           — direct REST API call (curl, Postman, scripts)
#   desktop       — Electron desktop app (web UI in BrowserWindow)
#   buddy         — Buddy/Cowork agent (CLI subprocess today; web client in future)
#
# Detection order:
#   1. X-AiNxt-Surface: desktop  (Electron webRequest interceptor injects this)
#   2. X-AiNxt-Client: cli/*     (CLI binary — checked BEFORE cowork surface so a
#                                  standalone CLI user whose config.toml still carries
#                                  x-ainxt-surface: cowork from a prior Buddy session
#                                  is correctly identified as CLI, not buddy)
#   3. X-AiNxt-Surface: cowork   (Buddy/Cowork agent surface — only reached when the
#                                  request does NOT carry an explicit cli/* client header)
#   4. X-AiNxt-Client header     (other explicit values — IDE, browser-agent, api, etc.)
#   5. /ide/* path prefix        (IDE router requests)
#   6. User-Agent heuristics     (fallback)
#   7. Default: platform
#
# The detected source is:
#   - Set in thread-local logger context (appears in every log line)
#   - Added to response as X-AiNxt-Client-Detected header (for debugging)
#   - Stored in request.state.client_source for downstream handlers
#
# Downstream channel derivation (model_usages.source_channel):
#   client_source  +  context                  →  source_channel
#   ─────────────────────────────────────────────────────────────
#   desktop                                    →  DESKTOP-CHAT / DESKTOP-IDE
#   buddy                                      →  DESKTOP-BUDDY  (the Buddy/Office
#                                                  surface is desktopOnly in the
#                                                  sidebar and reachable only via the
#                                                  Electron CLI subprocess — there is
#                                                  no web Buddy client, so every
#                                                  cowork-surfaced request is tagged
#                                                  DESKTOP-BUDDY unconditionally)
#   cli                                        →  CLI
#   platform                                   →  WEB-CHAT / WEB-IDE
# ============================================================

from __future__ import annotations

import re
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logger import set_client_source, clear_chat_context


# Canonical values
CLIENT_PLATFORM      = "platform"
CLIENT_CLI           = "cli"
CLIENT_IDE_VSCODE    = "ide-vscode"
CLIENT_IDE_JB        = "ide-jetbrains"
CLIENT_API           = "api"
# Browser-automation Chrome extension. Tagged via X-AiNxt-Client: browser-agent.
# NOTE: there is deliberately NO User-Agent fallback for this client — the
# extension sends the browser's default User-Agent, which is indistinguishable
# from the platform web UI (also a browser). The explicit header is the only
# reliable signal; correctness for the RAG-injection path is additionally
# guarded server-side by a request-shape heuristic (see gateway._gateway_stream).
CLIENT_BROWSER_AGENT = "browser-agent"
# Electron desktop app. The main process injects X-AiNxt-Surface: desktop via
# a webRequest.onBeforeSendHeaders interceptor so all gateway requests from the
# desktop BrowserWindow are distinguishable from plain browser traffic.
CLIENT_DESKTOP       = "desktop"
# Buddy/Cowork agent. Always a CLI subprocess spawned by the Electron desktop
# app (x-ainxt-surface: cowork injected via config.toml extra_headers) — the
# Buddy/Office sidebar entry is desktopOnly, so there is no web-based Buddy
# client. Downstream code tags every such request DESKTOP-BUDDY.
CLIENT_BUDDY         = "buddy"

# Header sent by ainxt-cli and IDE plugins
_HEADER = "x-ainxt-client"
# Surface header injected by the Electron desktop app and the Buddy/Cowork CLI
_SURFACE_HEADER = "x-ainxt-surface"

# User-Agent patterns → client_source
_UA_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ainxt-cli",           re.I), CLIENT_CLI),
    (re.compile(r"vscode|vscode-ainxt", re.I), CLIENT_IDE_VSCODE),
    (re.compile(r"jetbrains|pycharm|intellij|idea", re.I), CLIENT_IDE_JB),
    # curl/httpie/python-requests → treat as direct API
    (re.compile(r"curl|httpie|python-requests|insomnia|postman", re.I), CLIENT_API),
]


def _detect(request: Request) -> str:
    # 1. X-AiNxt-Surface: desktop — Electron desktop app injects this via
    #    webRequest.onBeforeSendHeaders on every BrowserWindow request so it is
    #    distinguishable from plain browser traffic. Checked first so the desktop
    #    is never accidentally classified as "platform".
    #    NOTE: the Electron interceptor only fires for BrowserWindow requests.
    #    The Buddy CLI subprocess makes its own TCP connections, so its
    #    x-ainxt-surface: cowork header arrives unmodified (not overwritten).
    surface = request.headers.get(_SURFACE_HEADER, "").strip().lower().split("/")[0]
    if surface == "desktop":
        return CLIENT_DESKTOP

    # 2. X-AiNxt-Client: cli/* — CLI binary always sends this header.
    #    Checked BEFORE x-ainxt-surface: cowork so that a standalone CLI user
    #    whose config.toml still carries x-ainxt-surface: cowork (written there
    #    by the desktop app during a prior Buddy session and never cleaned up)
    #    is correctly identified as CLIENT_CLI rather than CLIENT_BUDDY.
    #    Without this guard the middleware returned CLIENT_BUDDY → WEB-BUDDY in
    #    model_usages even though the request came from a plain terminal session.
    explicit = request.headers.get(_HEADER, "").strip().lower()
    if explicit:
        # Normalise: "cli/1.0.0" → "cli", "ide-vscode/0.9" → "ide-vscode"
        normalised = explicit.split("/")[0]
        if normalised == "cowork":
            # Older Buddy CLI — same semantics as x-ainxt-surface: cowork.
            return CLIENT_BUDDY
        # Any other explicit client header (cli, ide-vscode, ide-jetbrains,
        # browser-agent, api, …) wins over the surface header.
        return normalised

    # 3. X-AiNxt-Surface: cowork — Buddy/Cowork agent surface.
    #    Only reached when the request does NOT carry an explicit cli/* client
    #    header, meaning it is a genuine Buddy/Cowork subprocess or a future
    #    web-based Buddy client. Downstream code distinguishes DESKTOP-BUDDY
    #    (cli header present) vs WEB-BUDDY (no cli) — but since we already
    #    returned above when the cli header is present, every request that
    #    reaches here with surface=cowork is a true Buddy session.
    if surface == "cowork":
        return CLIENT_BUDDY

    # 4. IDE router path prefix
    if request.url.path.startswith("/ainxt/v1/api/ide"):
        return CLIENT_IDE_VSCODE   # all IDE router traffic assumed VS Code for now

    # 5. User-Agent heuristics
    ua = request.headers.get("user-agent", "")
    for pattern, source in _UA_PATTERNS:
        if pattern.search(ua):
            return source

    # 6. Default — browser / platform
    return CLIENT_PLATFORM


class ClientSourceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        source = _detect(request)

        # Make available to all downstream code
        request.state.client_source = source
        set_client_source(source)

        response = await call_next(request)

        # Echo back so clients / Grafana can see it
        response.headers["x-ainxt-client-detected"] = source

        # Clear thread-local after response (avoid bleed into next request)
        set_client_source("platform")
        return response
