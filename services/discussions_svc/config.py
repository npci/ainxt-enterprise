# SPDX-License-Identifier: MIT
# ============================================================
# DISCUSSIONS SVC — configuration
#
# Runs the @AiNxt bot: agent_bridge.py + worker.py (its own dedicated RQ
# worker process). There is no standalone FastAPI app here anymore — the
# gateway's own write path (routers/discussions_router.py) detects mentions
# and enqueues the job directly; nothing calls into this package over HTTP.
# See docs/DISCUSSIONS_MODULE_IMPLEMENTATION_PLAN.md ("Revision history",
# third architecture).
# ============================================================
import os

ENABLE_DISCUSSIONS = os.getenv("ENABLE_DISCUSSIONS", "false").lower() == "true"

DISCUSSIONS_BOT_AGENT_NAME = os.getenv("DISCUSSIONS_BOT_AGENT_NAME", "discussions_ainxt_bot")

# The @AiNxt bot authenticates against the headless engine exactly like any
# other AiNxt user — via core/discussions_assertion.py + core/discussions_engine_client.py
# — not via a separate API token. This is the claims payload used for that.
DISCUSSIONS_BOT_USER_CLAIMS = {
    "sub": os.getenv("DISCUSSIONS_BOT_SUB", "ainxt-system-bot"),
    "email": os.getenv("DISCUSSIONS_BOT_EMAIL", "ainxt-bot@ainxt.local"),
    "display_name": os.getenv("DISCUSSIONS_BOT_DISPLAY_NAME", "AiNxt"),
    "role": "user",
    "department": None,
    "ad_level": None,
}
