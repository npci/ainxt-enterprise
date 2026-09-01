# SPDX-License-Identifier: Apache-2.0
"""
Backend entry point — starts the FastAPI server with Uvicorn.

Usage (from the backend/ directory):
    python run.py

Server defaults:
    host  0.0.0.0   (all network interfaces)
    port  8002
    reload True     (hot-reload on code changes, for development)

The frontend (Vite, default port 5173) must be configured to proxy API
calls to http://localhost:8002 or set CORS_ALLOW_ORIGINS accordingly.

CORS / CORS_ALLOW_ORIGINS
--------------------------
app/main.py fails closed (SEC-F-030): if CORS_ALLOW_ORIGINS is unset it
always refuses to start — there is no dev-mode fallback to a hardcoded
localhost allow-list, in any environment. This entry point is
development-only, so it defaults CORS_ALLOW_ORIGINS to the usual local
frontend ports below (setdefault, so an explicit CORS_ALLOW_ORIGINS you set
yourself still wins). For anything other than local work, set
CORS_ALLOW_ORIGINS to the real deployed origin(s) instead.

Note: this runs ABStudio standalone, where app/api/deps.py cannot import the
gateway's auth package and falls back to a stub that returns a hardcoded
admin user with no authentication. Keep it on a trusted local network.
"""
import os
import sys
import asyncio

# Default the local-dev CORS allow-list for this dev-only entry point.
# setdefault so an explicit CORS_ALLOW_ORIGINS from the environment still wins.
os.environ.setdefault(
    "CORS_ALLOW_ORIGINS",
    "http://localhost:5173,http://localhost:5174,http://localhost:3000",
)

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8002,
        reload=True,
        log_level="info",
    )
