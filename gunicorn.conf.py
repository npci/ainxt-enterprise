# SPDX-License-Identifier: MIT
# ============================================================
# Gunicorn production configuration
# Target: 4 vCPU / 8 GB RAM → 9 workers (2×CPU+1)
#
# Start with:
#   gunicorn gateway:app -c gunicorn.conf.py
# ============================================================

import multiprocessing
import os

# ── Workers ─────────────────────────────────────────────────
workers        = int(os.getenv("WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class   = "uvicorn.workers.UvicornWorker"
worker_connections = 1000    # max concurrent connections per worker

# ── Network ─────────────────────────────────────────────────
bind           = os.getenv("BIND", "0.0.0.0:8000")
backlog        = 2048        # pending connection queue

# ── Timeouts ────────────────────────────────────────────────
timeout        = 240         # worker silent timeout (seconds) — raised from 120→240 to cover large attachment sends (Graph can take up to 120s + relay overhead = ~135s total)
graceful_timeout = 30        # extra grace on SIGTERM before SIGKILL
keepalive      = 5           # keep-alive connection timeout

# ── Process management ───────────────────────────────────────
max_requests   = 1000        # restart worker after N requests (memory leak guard)
max_requests_jitter = 100    # add jitter to prevent all workers restarting at once
preload_app    = False       # False: each worker gets fresh Python state

# ── Logging ─────────────────────────────────────────────────
loglevel       = os.getenv("LOG_LEVEL", "info")

# Log destination.
#
# These were hardcoded to /app/log/{access,error}.log. That path only exists
# inside the container image, and `log/` is gitignored so the directory is not
# in the repository at all — so `gunicorn gateway:app -c gunicorn.conf.py`, the
# start command documented in the README and at the top of this file, aborted
# before binding a socket with:
#     Error: '/app/log/error.log' isn't writable [FileNotFoundError]
#
# Now: GUNICORN_LOG_DIR chooses the directory, defaulting to ./log relative to
# wherever the server is started. If it cannot be created or written, fall back
# to stdout/stderr ("-") rather than refusing to boot — a server that logs to
# the console is strictly better than one that will not start.
_log_dir = os.getenv("GUNICORN_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "log"))
try:
    os.makedirs(_log_dir, exist_ok=True)
    _probe = os.path.join(_log_dir, ".write-probe")
    with open(_probe, "a"):
        pass
    os.unlink(_probe)
except OSError:
    _log_dir = None

accesslog      = os.path.join(_log_dir, "access.log") if _log_dir else "-"
errorlog       = os.path.join(_log_dir, "error.log") if _log_dir else "-"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sµs'

# ── Security ────────────────────────────────────────────────
limit_request_line   = 8190
limit_request_fields = 100

# ── Process hooks ────────────────────────────────────────────
def post_fork(server, worker):
    """
    Re-initialise log handlers in each worker after the fork.

    Without this hook, forked workers inherit the parent process's open
    file descriptors.  On Linux, multiple processes sharing the same fd
    can cause writes to interleave (corrupted JSON lines) and, after a
    rotation, some workers will continue writing to the renamed/rotated
    file rather than the new agent.log.

    By closing and re-opening all FileHandler instances after the fork,
    each worker gets its own independent fd pointing to the current
    agent.log, and inode-change detection in SizeAndTimeRotatingFileHandler
    works correctly from the start of the worker's life.
    """
    import logging
    import sys

    ainxt_logger = logging.getLogger("ainxt")

    # Close and remove every handler inherited from the parent process
    for handler in list(ainxt_logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        ainxt_logger.removeHandler(handler)

    # Also clear any FileHandlers from the root logger left by third-party
    # libraries or legacy code (e.g. logging_config.py era handlers)
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if isinstance(handler, logging.FileHandler):
            try:
                handler.close()
            except Exception:
                pass
            root_logger.removeHandler(handler)

    # Reset the per-process guard so _configure_logging_once() runs fresh in
    # this worker.  core.logger is already in sys.modules (imported by the
    # parent before fork), so this is a cheap attribute write + dict lookup.
    try:
        import core.logger as _core_logger
        _core_logger._LOGGING_CONFIGURED = False

        # Re-run the single, canonical setup path.
        # This adds the rotating FileHandler to ainxt and the StreamHandler to
        # root — exactly the same handlers the parent had, but with fresh fds
        # pointing to the current agent.log inode.
        _core_logger._configure_logging_once()

        logging.getLogger("ainxt").info(
            f"post_fork: log handlers re-initialised in worker pid={worker.pid}"
        )
    except Exception:
        # Last-resort: at minimum keep stdout so errors are visible
        fallback = logging.StreamHandler(sys.stdout)
        fallback.setLevel(logging.WARNING)
        ainxt_logger.addHandler(fallback)
        ainxt_logger.warning(
            f"post_fork: could not re-init file handler in worker pid={worker.pid}"
        )
