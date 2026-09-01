# SPDX-License-Identifier: Apache-2.0
# ============================================================
# PRODUCTION-GRADE LOGGER
# AiNxt AI Gateway Logging System
# Structlog + combined size-AND-time rotation
# Whichever limit is hit first triggers rotation.
#
# Multi-process safe design:
#   - SizeAndTimeRotatingFileHandler detects inode changes
#     (caused by peer processes rotating the file) and re-opens
#     the new log file automatically — no missed records.
#   - A threading.Lock guards shouldRollover()+doRollover() to
#     prevent concurrent threads in the same process from
#     double-rotating and leaving self.stream = None.
#   - cache_logger_on_first_use=False ensures structlog always
#     routes through live handlers rather than a frozen pipeline.
# ============================================================

import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
from contextvars import ContextVar

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


# ----- logging init guard (per-process) -----
_LOGGING_CONFIGURED = False

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
_SERVICE  = os.getenv("SERVICE_NAME", "ainxt-gateway")

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
LOG_ROTATION_WHEN     = os.getenv("LOG_ROTATION_WHEN",         "midnight")
LOG_ROTATION_INTERVAL = int(os.getenv("LOG_ROTATION_INTERVAL", "1"))
LOG_ROTATION_UTC      = os.getenv("LOG_ROTATION_UTC",          "false").lower() == "true"


# ============================================================
# COMBINED HANDLER  —  size OR time, whichever fires first
# Multi-process safe: detects inode change after peer rotation
# Thread safe: lock guards check-and-rotate sequence
# ============================================================

class SizeAndTimeRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """
    Rotates when EITHER condition is met first:
      - file size reaches LOG_MAX_BYTES
      - time-based schedule fires (midnight / hourly / etc.)

    Multi-process safety:
      On Linux, multiple gunicorn workers and RQ worker processes all write
      to the same log file.  When any one process rotates the file, the
      others must detect the inode change and re-open the new file.  This
      is done by checking os.stat(baseFilename) vs the fd's fstat on every
      emit() call (the same mechanism used by WatchedFileHandler).

    Thread safety:
      shouldRollover() + doRollover() are not atomic in the stdlib base
      class.  A lock ensures only one thread per process triggers rotation
      at a time, preventing self.stream from being left as None.

    Rotated filenames:
        agent.log.2026-03-23          (midnight)
        agent.log.2026-03-23_06       (hourly)
        agent.log.2026-03-23_06-30-00 (size burst mid-period)
    """

    def __init__(self, filename, max_bytes, **kwargs):
        super().__init__(filename, **kwargs)
        self.maxBytes = max_bytes
        self._rollover_lock = threading.Lock()
        # Track inode/device for multi-process rotation detection (Linux only)
        self._dev, self._ino = -1, -1
        self._stat_stream()

    # ------------------------------------------------------------------
    # inode tracking helpers  (no-op on Windows where stat has no ino)
    # ------------------------------------------------------------------

    def _stat_stream(self):
        """Record the inode of the currently open stream."""
        if self.stream is None:
            return
        try:
            sres = os.fstat(self.stream.fileno())
            self._dev = sres.st_dev
            self._ino = sres.st_ino
        except (OSError, AttributeError):
            pass

    def _reopen_if_rotated(self):
        """
        If the file on disk no longer matches the open fd (another process
        renamed/rotated it), close the stale fd and open the new file.
        This is the core of WatchedFileHandler adapted for Linux.
        On Windows st_ino is always 0 so the check is skipped safely.
        """
        if self.stream is None or self._ino == -1:
            return
        try:
            sres = os.stat(self.baseFilename)
            if sres.st_dev != self._dev or sres.st_ino != self._ino:
                # Peer process rotated the file — re-open
                self.stream.flush()
                self.stream.close()
                self.stream = self._open()
                self._stat_stream()
        except FileNotFoundError:
            # File was removed — re-open (creates a new file)
            if self.stream:
                self.stream.close()
            self.stream = self._open()
            self._stat_stream()
        except (OSError, AttributeError):
            pass  # Windows or permission error — skip silently

    # ------------------------------------------------------------------
    # shouldRollover — thread-safe size + time check
    # ------------------------------------------------------------------

    def shouldRollover(self, record) -> bool:
        """Check size first (cheap seek), then fall through to time check."""
        # Size check — seek only if stream is open and maxBytes is configured
        if self.stream and self.maxBytes > 0:
            try:
                self.stream.seek(0, 2)
                if self.stream.tell() >= self.maxBytes:
                    return True
            except OSError:
                pass
        # Fall through to time-based check
        return super().shouldRollover(record)

    # ------------------------------------------------------------------
    # emit — re-open if a peer process rotated, then write
    # ------------------------------------------------------------------

    def emit(self, record):
        """
        Emit a log record.

        Before writing, check whether a peer process has rotated the file
        (inode mismatch).  If so, re-open and write to the new file.
        The shouldRollover + doRollover path is guarded by a lock so
        concurrent threads cannot double-rotate and corrupt self.stream.
        """
        try:

            # Step 1: check whether THIS process should rotate (lock-protected)
            with self._rollover_lock:
                self._reopen_if_rotated()

                if self.shouldRollover(record):
                    self.doRollover()
                    self._stat_stream()   # record new inode after our own rotation
                
                if self.stream is None:
                    self.stream = self._open()
                    self._stat_stream()

            # Step 2: write the record via FileHandler (skips shouldRollover again)
            logging.FileHandler.emit(self, record)
            self.flush()

        except Exception as e:            
            try:
                sys.stderr.write(f"[LOGGER FAILURE] {e}\n")
            except Exception:
                pass

            self.handleError(record)


# ============================================================
# PER-COROUTINE CONTEXT  (ContextVar — asyncio-safe)
# ============================================================
# Previously this used threading.local(), which is keyed by OS thread.
# Uvicorn runs all async coroutines on a single event-loop thread, so
# concurrent coroutines shared the same thread-local storage — a coroutine
# suspended at `await` could have its request_id overwritten by another
# coroutine that ran while it was waiting.
#
# ContextVar is keyed by asyncio Task (coroutine), not by OS thread.
# Each Task gets its own copy of every ContextVar, so concurrent coroutines
# on the same thread can never overwrite each other's context.
#
# asyncio automatically copies the current ContextVar snapshot into
# run_in_executor() threads, so NeMo/compliance offloads inherit the
# correct request_id without any extra work.
#
# For background threading.Thread daemons (history save, budget, eval),
# callers must capture contextvars.copy_context() at spawn time and run
# the target inside ctx.run(...) — see gateway.py and messages_compat_router.py.
# ============================================================
# Async-safe context storage. Was previously ``threading.local()`` — that is
# NOT safe under FastAPI/anyio, where many concurrent requests share the
# same event-loop thread and would clobber each other's fields (notably
# ``client_source`` — a request that resolved to "platform" would overwrite
# a concurrent browser-agent request's value between the middleware's
# ``set_client_source`` and the handler's log emission).
#
# ``contextvars.ContextVar`` is the correct primitive: FastAPI runs each
# request in its own ``contextvars.Context`` (via anyio), so writes are
# isolated per request. RQ workers, threaded executors and ``sdlc_log_context``
# also observe correct scoping because a fresh thread starts with a fresh,
# empty context and ``ContextVar.set()`` on that thread does not leak into
# the parent request's context.
#
# The default value on each ``.get()`` mirrors the previous
# ``getattr(_log_context, name, default)`` semantics exactly.
from contextvars import ContextVar

_cv_request_id:     ContextVar[str] = ContextVar("ainxt_log_request_id",     default="-")
_cv_user_id:        ContextVar[str] = ContextVar("ainxt_log_user_id",        default="-")
_cv_chat_id:        ContextVar[str] = ContextVar("ainxt_log_chat_id",        default="-")
_cv_span_id:        ContextVar[str] = ContextVar("ainxt_log_span_id",        default="-")
_cv_client_source:  ContextVar[str] = ContextVar("ainxt_log_client_source",  default="platform")
_cv_job_kind:       ContextVar[str] = ContextVar("ainxt_log_job_kind",       default="")
_cv_agent_id:       ContextVar[str] = ContextVar("ainxt_log_agent_id",       default="")
_cv_pipeline_stage: ContextVar[str] = ContextVar("ainxt_log_pipeline_stage", default="")
_cv_task_id:        ContextVar[str] = ContextVar("ainxt_log_task_id",        default="")
_cv_correlation_id: ContextVar[str] = ContextVar("ainxt_log_correlation_id", default="")


def set_request_id(request_id: str) -> None:
    _cv_request_id.set(request_id)



def get_request_id() -> str:
    return _cv_request_id.get()


def set_chat_context(user_id: str, chat_id: str):
    _cv_user_id.set(user_id)
    _cv_chat_id.set(chat_id)


def set_span_id(span_id: str):
    _cv_span_id.set(span_id)


def get_span_id() -> str:
    return _cv_span_id.get()


def set_client_source(source: str):
    """Set the client source for this request: platform | cli | ide-vscode | ide-jetbrains | browser-agent"""
    _cv_client_source.set(source)


def get_client_source() -> str:
    return _cv_client_source.get()


def clear_chat_context():
    _cv_user_id.set("-")
    _cv_chat_id.set("-")
    _cv_span_id.set("-")
    _cv_client_source.set("platform")


def get_user_id() -> str:
    return _cv_user_id.get()


def get_chat_id() -> str:
    return _cv_chat_id.get()


def set_job_kind(job_kind: str = ""):
    _cv_job_kind.set(job_kind or "")


def get_job_kind() -> str:
    return _cv_job_kind.get()


def get_agent_id() -> str:
    return _cv_agent_id.get()


def get_task_id() -> str:
    return _cv_task_id.get()


def get_correlation_id() -> str:
    return _cv_correlation_id.get()


def bind_context(
    agent_id: str = "",
    pipeline_stage: str = "",
    task_id: str = "",
    correlation_id: str = "",
    job_kind: str = "",
):
    # Preserve previous "empty argument = keep existing value" semantics.
    if agent_id:
        _cv_agent_id.set(agent_id)
    if pipeline_stage:
        _cv_pipeline_stage.set(pipeline_stage)
    if task_id:
        _cv_task_id.set(task_id)
    if correlation_id:
        _cv_correlation_id.set(correlation_id)
    if job_kind:
        _cv_job_kind.set(job_kind)


def set_correlation_id(correlation_id: str = ""):
    """Unconditionally set (or clear) the per-request correlation_id.

    Unlike bind_context(), this does NOT fall back to the existing value when
    given an empty string — it always overwrites. Use this at request entry
    points so a request never inherits a stale correlation_id (contextvars
    make cross-request leaks impossible under FastAPI, but this helper is
    still the right primitive for tasks that run outside a fresh Context —
    e.g. RQ workers reusing a Python thread — where the previous run's
    value could otherwise remain visible).
    """
    _cv_correlation_id.set(correlation_id or "")


def clear_bound_context():
    _cv_agent_id.set("")
    _cv_pipeline_stage.set("")
    _cv_task_id.set("")
    _cv_correlation_id.set("")
    _cv_job_kind.set("")


class sdlc_log_context:
    """Context manager that binds SDLC correlation context for the current thread.

    Usage:
        with sdlc_log_context(run_id, "sdlc_feature"):
            ...  # all logging inside has correlation_id + pipeline_stage

    Safe for RQ workers, daemon threads, and ThreadPoolExecutor — each
    thread gets its own context via thread-local storage.
    """

    __slots__ = ("_run_id", "_stage")

    def __init__(self, run_id: str, pipeline_stage: str = "sdlc"):
        self._run_id = run_id or ""
        self._stage  = pipeline_stage

    def __enter__(self):
        bind_context(correlation_id=self._run_id, pipeline_stage=self._stage)
        return self

    def __exit__(self, *exc):
        clear_bound_context()
        return False


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
    event_dict.setdefault("client_source",  get_client_source())
    event_dict.setdefault("agent_id",       _cv_agent_id.get())
    event_dict.setdefault("pipeline_stage", _cv_pipeline_stage.get())
    event_dict.setdefault("task_id",        _cv_task_id.get())
    event_dict.setdefault("correlation_id", _cv_correlation_id.get())
    event_dict.setdefault("job_kind",       _cv_job_kind.get())
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
        errors="replace",
        utc=LOG_ROTATION_UTC,
        delay=False,   # open immediately so _stat_stream() captures the inode at startup
    )
    handler.setLevel(LOG_LEVEL)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _configure_logging_once():
    global _LOGGING_CONFIGURED
    
    if _LOGGING_CONFIGURED:
        return
        
    _LOGGING_CONFIGURED = True    

    # ── Named app logger ──────────────────────────────────────
    app_logger = logging.getLogger(LOGGER_NAME)
    app_logger.setLevel(LOG_LEVEL)
    app_logger.propagate = False        # prevent double-logging to root

    # Only add handler if not already present
    if not _handler_exists(app_logger, LOG_FILE):
        app_logger.addHandler(_make_rotating_handler())


    # ── Root logger (console only — no duplicate file writes) ─
    # NOTE: The "ainxt" named logger writes ONLY to agent.log (no stdout).
    #       Third-party library logs reach stdout via the root logger below.
    root_logger = logging.getLogger()
    root_logger.setLevel(LOG_LEVEL)
    
    # Clean up any existing console handlers to prevent duplicates
    console_handlers = [h for h in root_logger.handlers if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout]
    for handler in console_handlers:
        root_logger.removeHandler(handler)
    
    # Add console handler
    root_console = logging.StreamHandler(sys.stdout)
    root_console.setLevel(LOG_LEVEL)
    root_console.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(root_console)

    # ── Structlog pipeline ────────────────────────────────────
    # cache_logger_on_first_use=False: ensures the pipeline is never frozen
    # against a stale handler reference.  The micro-overhead (~1-2 µs/call)
    # is negligible compared to LLM network latency.
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
        cache_logger_on_first_use=False,   # FIX: was True — prevented pipeline refresh after rotation
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

def mask_email(email: str) -> str:
      """Mask an email address for safe logging.

      Keeps up to 2 chars of the local part (never the full local part),
      masks the rest, preserves the domain.
          alice@corp.com        →  al***@corp.com
          a@corp.com            →  a***@corp.com
          notanemail            →  notanemail   (returned as-is)
          None / ""             →  ""           (no crash)
      """
      if not email or not isinstance(email, str):
          return "" if not email else str(email)
      if "@" not in email:
          return email
      local, domain = email.rsplit("@", 1)   # rsplit handles multiple @ safely
      if not local:
          return f"***@{domain}"
      visible_len = min(2, max(1, len(local) - 1))  # never expose full local part
      return f"{local[:visible_len]}***@{domain}"