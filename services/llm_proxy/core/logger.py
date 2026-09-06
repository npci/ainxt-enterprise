# SPDX-License-Identifier: MIT
# ============================================================
# PRODUCTION-GRADE LOGGER
# AiNxt AI Gateway Logging System
# Structlog + combined size-AND-time rotation
# Whichever limit is hit first triggers rotation.
# ============================================================

import logging
import logging.handlers
import os
import socket
import sys
import threading
import time

# ============================================================
# TIMEZONE CONFIG  —  set before any datetime import
# ============================================================
# LOG_TIMEZONE  — IANA timezone name (default "Asia/Kolkata" = IST UTC+5:30)
#   "Asia/Kolkata"     → IST  UTC+5:30   (default)
#   "UTC"              → UTC
#   "America/New_York" → EST/EDT
#   "Europe/London"    → GMT/BST
# ─────────────────────────────────────────────────────────────
LOG_TIMEZONE = os.getenv("LOG_TIMEZONE", "Asia/Kolkata")
os.environ["TZ"] = LOG_TIMEZONE
try:
    time.tzset()   # applies on Linux/macOS — no-op on Windows
except AttributeError:
    pass           # Windows — TZ env var still read by some libs

import structlog


# ============================================================
# CONFIG  —  all values overridable via environment variables
# ============================================================

LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL      = getattr(logging, LOG_LEVEL_NAME, logging.INFO)

LOGGER_NAME = "ainxt"

DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "log", "app")
_LOG_DIR = os.getenv("LOG_DIR", DEFAULT_LOG_DIR)
os.makedirs(_LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(_LOG_DIR, "agent.log")

_HOSTNAME = socket.gethostname()
_SERVICE  = os.getenv("SERVICE_NAME", "ainxt-llm-proxy")

# ── Size rotation ─────────────────────────────────────────────────────────────
# LOG_MAX_BYTES      — rotate when file reaches this size   (default 50 MB)
# LOG_BACKUP_COUNT   — total rotated files to keep          (default 30)
# ─────────────────────────────────────────────────────────────────────────────
LOG_MAX_BYTES    = int(os.getenv("LOG_MAX_BYTES",    str(50 * 1024 * 1024)))  # 50 MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "30"))

# ── Time rotation ─────────────────────────────────────────────────────────────
# LOG_ROTATION_WHEN      — rotation schedule               (default "midnight")
#   "midnight"  rotate once per day at midnight
#   "h"         every N hours
#   "d"         every N days
#   "w0"–"w6"   weekly: w0=Monday … w6=Sunday
#   "s"         every N seconds (testing only)
#
# LOG_ROTATION_INTERVAL  — units between rotations         (default 1)
#   used only for "h", "d", "s"
#   e.g. WHEN=h INTERVAL=6 → every 6 hours
#
# LOG_ROTATION_UTC       — use UTC for rollover timing     (default "false")
#   "false" → rotation fires at midnight/hour in LOG_TIMEZONE
#   "true"  → rotation fires at midnight/hour in UTC
# ─────────────────────────────────────────────────────────────────────────────
LOG_ROTATION_WHEN     = os.getenv("LOG_ROTATION_WHEN",         "h")
LOG_ROTATION_INTERVAL = int(os.getenv("LOG_ROTATION_INTERVAL", "1"))
LOG_ROTATION_UTC      = os.getenv("LOG_ROTATION_UTC",          "false").lower() == "true"


# ============================================================
# COMBINED HANDLER  —  size OR time, whichever fires first
# ============================================================

class SizeAndTimeRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """
    Rotates when EITHER condition is met first:
      - file size reaches LOG_MAX_BYTES
      - time-based schedule fires (midnight / hourly / etc.)

    Rotated filenames:
        agent.log.2026-03-23          (midnight)
        agent.log.2026-03-23_06       (hourly)
        agent.log.2026-03-23_06-30-00 (size burst mid-period)
    """

    def __init__(self, filename, max_bytes, **kwargs):
        super().__init__(filename, **kwargs)
        self.maxBytes = max_bytes

    def shouldRollover(self, record) -> bool:
        # Size check first — cheap seek to end of file
        if self.stream and self.maxBytes > 0:
            self.stream.seek(0, 2)
            if self.stream.tell() >= self.maxBytes:
                return True
        # Fall through to time-based check
        return super().shouldRollover(record)


# ============================================================
# THREAD-LOCAL CONTEXT
# ============================================================

_log_context = threading.local()


def set_request_id(request_id: str):
    _log_context.request_id = request_id


def get_request_id() -> str:
    return getattr(_log_context, "request_id", "-")


def set_chat_context(user_id: str, chat_id: str):
    _log_context.user_id = user_id
    _log_context.chat_id = chat_id


def set_conv_id(conv_id: str):
    """Bind the stable per-conversation ID (x-ainxt-conv-id) for this request."""
    _log_context.conv_id = conv_id


def get_conv_id() -> str:
    return getattr(_log_context, "conv_id", "-")


def set_span_id(span_id: str):
    _log_context.span_id = span_id


def get_span_id() -> str:
    return getattr(_log_context, "span_id", "-")


def set_client_source(source: str):
    """Set the client source for this request: platform | cli | ide-vscode | ide-jetbrains"""
    _log_context.client_source = source


def get_client_source() -> str:
    return getattr(_log_context, "client_source", "platform")


def clear_chat_context():
    _log_context.user_id = "-"
    _log_context.chat_id = "-"
    _log_context.conv_id = "-"
    _log_context.span_id = "-"
    _log_context.client_source = "platform"


def get_user_id() -> str:
    return getattr(_log_context, "user_id", "-")


def get_chat_id() -> str:
    return getattr(_log_context, "chat_id", "-")


def bind_context(
    agent_id: str = "",
    pipeline_stage: str = "",
    task_id: str = "",
    correlation_id: str = "",
):
    _log_context.agent_id       = agent_id       or getattr(_log_context, "agent_id", "")
    _log_context.pipeline_stage = pipeline_stage or getattr(_log_context, "pipeline_stage", "")
    _log_context.task_id        = task_id        or getattr(_log_context, "task_id", "")
    _log_context.correlation_id = correlation_id or getattr(_log_context, "correlation_id", "")


def clear_bound_context():
    _log_context.agent_id       = ""
    _log_context.pipeline_stage = ""
    _log_context.task_id        = ""
    _log_context.correlation_id = ""


# ============================================================
# STRUCTLOG CONTEXT PROCESSOR
# ============================================================

def _context_processor(_logger, _method_name, event_dict):
    event_dict.setdefault("service",        _SERVICE)
    event_dict.setdefault("host",           _HOSTNAME)
    event_dict.setdefault("request_id",     get_request_id())
    event_dict.setdefault("span_id",        get_span_id())
    event_dict.setdefault("user_id",        get_user_id())
    event_dict.setdefault("chat_id",        get_chat_id())
    event_dict.setdefault("conv_id",        get_conv_id())
    event_dict.setdefault("client_source",  get_client_source())
    event_dict.setdefault("agent_id",       getattr(_log_context, "agent_id", ""))
    event_dict.setdefault("pipeline_stage", getattr(_log_context, "pipeline_stage", ""))
    event_dict.setdefault("task_id",        getattr(_log_context, "task_id", ""))
    event_dict.setdefault("correlation_id", getattr(_log_context, "correlation_id", ""))
    return event_dict


# ============================================================
# LOGGING SETUP
# ============================================================

def _handler_exists(logger_obj: logging.Logger, filename: str) -> bool:
    for h in logger_obj.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == filename:
            return True
    return False


def _make_rotating_handler() -> SizeAndTimeRotatingFileHandler:
    handler = SizeAndTimeRotatingFileHandler(
        filename=LOG_FILE,
        max_bytes=LOG_MAX_BYTES,
        when=LOG_ROTATION_WHEN,
        interval=LOG_ROTATION_INTERVAL,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        utc=LOG_ROTATION_UTC,
    )
    handler.setLevel(LOG_LEVEL)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _configure_logging_once():
    # ── Named app logger ──────────────────────────────────────
    app_logger = logging.getLogger(LOGGER_NAME)
    app_logger.setLevel(LOG_LEVEL)
    app_logger.propagate = False        # prevent double-logging to root

    if not _handler_exists(app_logger, LOG_FILE):
        app_logger.addHandler(_make_rotating_handler())

    # ── Root logger (console only — no duplicate file writes) ─
    # NOTE: The "ainxt" named logger writes ONLY to agent.log (no stdout).
    #       Third-party library logs reach stdout via the root logger below.
    root_logger = logging.getLogger()
    root_logger.setLevel(LOG_LEVEL)
    if not any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
        for h in root_logger.handlers
    ):
        root_console = logging.StreamHandler(sys.stdout)
        root_console.setLevel(LOG_LEVEL)
        root_console.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(root_console)

    # ── Structlog pipeline ────────────────────────────────────
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _context_processor,
            structlog.stdlib.add_logger_name,
            structlog.processors.add_log_level,
            # utc=False → timestamps use LOG_TIMEZONE (IST by default)
            structlog.processors.TimeStamper(fmt="iso", key="timestamp", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(serializer=lambda obj, **kw: __import__("json").dumps(obj, ensure_ascii=False, **kw)),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(LOG_LEVEL),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


_configure_logging_once()
logger = structlog.get_logger(LOGGER_NAME)


# ============================================================
# SAFE LOGGING HELPERS
# ============================================================

def log_info(message: str):
    logger.info(message)


def log_error(message: str, exc: Exception = None):
    if exc:
        logger.exception(message)
    else:
        logger.error(message)


def log_warning(message: str):
    logger.warning(message)


def log_debug(message: str):
    logger.debug(message)


logger.info(
    "logger_initialized",
    log_file=LOG_FILE,
    timezone=LOG_TIMEZONE,
    log_level=LOG_LEVEL_NAME,
    rotation_when=LOG_ROTATION_WHEN,
    rotation_interval=LOG_ROTATION_INTERVAL,
    rotation_max_bytes=LOG_MAX_BYTES,
    rotation_backup_count=LOG_BACKUP_COUNT,
    rotation_utc=LOG_ROTATION_UTC,
)
