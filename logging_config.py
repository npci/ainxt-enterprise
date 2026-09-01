# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DEPRECATED — DO NOT USE
#
# This file is superseded by core/logger.py.
# It is retained only to avoid ImportError in any code that
# may reference it, but it is NOT imported anywhere in the
# active codebase (verified by grep).
#
# What was wrong with this file:
#
#   1. It called structlog.configure() at module-import time,
#      overwriting the richer pipeline already set up by
#      core/logger.py (missing context enrichment, no
#      request_id/user_id/chat_id injection, no stack trace
#      rendering).
#
#   2. It registered a plain logging.FileHandler (no rotation)
#      on the ROOT logger.  On Linux this handler held an open
#      fd to agent.log indefinitely.  When core/logger.py's
#      SizeAndTimeRotatingFileHandler renamed agent.log during
#      rotation, this stale fd kept writing to the renamed
#      (rotated) file — log records were silently lost from
#      the perspective of Promtail which had already moved on
#      to tailing the new agent.log.  On Windows it held an
#      exclusive write-lock, causing TimedRotatingFileHandler
#      to fail the rename silently, leaving log rotation
#      permanently broken until the process was restarted.
#
# All structured logging must go through:
#   from core.logger import logger
# ============================================================

# Nothing is configured here.  Importing this module is a no-op.
