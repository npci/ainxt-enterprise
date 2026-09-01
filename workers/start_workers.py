#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# START WORKERS — launch rq workers for AiNxt platform
#
# PRODUCTION WORKER POOLS (run each in a separate terminal / systemd unit):
#
#   # Chat workers — interactive, latency-sensitive (30 processes)
#   python workers/start_workers.py --chat --n 30
#
#   # SDLC workers — LLM-heavy, long-running (10 processes)
#   python workers/start_workers.py --sdlc --n 10
#
#   # Index workers — CPU/IO, codebase indexing (20 processes)
#   # Each worker runs 4 concurrent embed threads; 5 workers ≈ 20 embed calls in-flight at once
#   python workers/start_workers.py --index --n 20
#
#   # Agent workers — agentic pipelines (10 processes)
#   python workers/start_workers.py --agent --n 10
#
#   # KB ingest workers — document parsing (5 processes)
#   python workers/start_workers.py --kb --n 5
#
#   # Doc generation workers — parallel doc/pptx/pdf/xlsx generation (8 processes)
#   # Each doc job: LLM call (Claude Sonnet, ~30s-3min) + file render (~2-5s).
#   # Doc jobs are CPU+network bound, NOT IO-bound like chat.
#   # 8 workers = 8 concurrent LLM calls to Claude proxy — more than this
#   # saturates the proxy and causes cascading timeouts across ALL queues.
#   # Chat workers (10) are IO-bound SSE streams; doc workers are batch jobs.
#   python workers/start_workers.py --doc --n 8
#
#   # Dev / all queues (single process, all queues)
#   python workers/start_workers.py
#
# Resource profiles:
#   chat  — IO-bound (LLM API calls), high concurrency OK
#   sdlc  — LLM-heavy, long TTL, low concurrency
#   index — CPU+IO (AST chunk + embed), medium concurrency
#   agent — Orchestrator loops, medium concurrency
#   kb    — File parse + embed, low concurrency
# ============================================================

import argparse
import multiprocessing
import os
import signal
import sys
import threading
import time as _time

# Required on macOS to prevent objc_initializeAfterForkError when rq forks work-horses
os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")

# Add project root to path so imports work correctly
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# Also add ABStudio/backend so RQ workers can import `app.*` — mirrors the
# same insertion the gateway process does in gateway.py. Required so
# `app.services.trigger_scheduler.fire_from_queue` (enqueued by the trigger
# dispatcher) resolves inside the RQ worker's importlib.import_module call.
_ABS_BACKEND = os.path.join(_REPO_ROOT, "ABStudio", "backend")
if os.path.isdir(_ABS_BACKEND) and _ABS_BACKEND not in sys.path:
    sys.path.insert(0, _ABS_BACKEND)

# Load .env file before anything else (does not override existing shell env vars)
try:
    from dotenv import load_dotenv
    current_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(current_dir, "..", ".env")  # one directory up
    load_dotenv(dotenv_path=env_path, override=True)
except ImportError:
    pass

# CKMS — decrypt protected env vars (Redis password, Postgres password,
# provider keys, …) before any worker code imports core.config / db.database.
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

from core.job_queue import (
    ALL_QUEUES, Q_HIGH, Q_DEFAULT,
    Q_CHAT, Q_SDLC, Q_AGENT, Q_INDEX, Q_KB, Q_SECURITY, Q_DOC, Q_CODEWIKI, Q_EXEC, Q_CONNECTOR, Q_COACH,
    _rq_available, _env_int
)
from core.kv.queue import get_job_connection as _kv_get_job_connection, get_worker as _kv_get_worker
from core.logger import logger


def _worker_timeout_for(queue_names: list) -> int:
    if queue_names == [Q_KB]:
        return -1
    if queue_names == [Q_CODEWIKI]:
        # A full CodeWiki generation on a large repo can legitimately take up
        # to ~2 days (thousands of modules, each its own multi-step LLM agent
        # run -- see docs/codewiki-server-deployment.md). No finite cap here
        # matches enqueue_codewiki_job()'s own timeout=None; same pattern as
        # the existing Q_KB special case just above.
        return -1
    return 2100


def _worker_process(queue_names: list, burst: bool = False):
    """
    Target function for each worker subprocess.

    Receives plain queue name strings (picklable) and creates its own
    Queue/Worker objects inside the child process via the KV factory.
    This avoids the 'cannot pickle _thread.lock' error on macOS/Python 3.12
    where multiprocessing uses spawn (not fork).

    The worker itself is built by core.kv.queue.get_worker, which
    resolves the backend from REDIS_CLIENT_CONFIG_DB5.

    job_execution_timeout: hard cap per job inside the work-horse.
    Dedicated KB ingest workers run without an RQ worker-level cap because
    Docling parsing can exceed fixed caps; all other queues keep the 2100s
    safety net so hung non-KB jobs do not pin workers indefinitely.
    """
    try:
        # Build the worker via the KV factory rather than hard-coding
        # redis here, so REDIS_CLIENT_CONFIG_DB5 stays authoritative.
        # Each child process constructs its own worker after the spawn —
        # connections are NOT inherited across the fork/spawn boundary
        # (this breaks under macOS spawn).
        worker = _kv_get_worker(queue_names, job_execution_timeout=_worker_timeout_for(queue_names))
        if worker is None:
            raise RuntimeError(f"Worker init failed for queues: {queue_names}")
        logger.info(f"Worker started — queues: {queue_names}")
        try:
            worker.work(burst=burst, with_scheduler=True)
        except TypeError:
            # Older / RC worker may not accept with_scheduler kwarg
            worker.work(burst=burst)
    except Exception as e:
        logger.error(f"Worker crashed: {e}")
        raise


def start_worker(queue_names: list, burst: bool = False):
    """Start a single Worker in the current process.

    Uses the same queue-specific job_execution_timeout as the subprocess
    workers spawned by ``start_n_workers`` so behaviour is identical
    regardless of how operators launch workers.
    """
    if not _rq_available:
        logger.error("Queue backend unavailable — cannot start workers")
        sys.exit(1)

    worker = _kv_get_worker(queue_names, job_execution_timeout=_worker_timeout_for(queue_names))
    if worker is None:
        logger.error(f"Worker init failed for queues: {queue_names}")
        sys.exit(1)
    logger.info(f"Starting worker consuming: {queue_names}")
    try:
        worker.work(burst=burst, with_scheduler=True)
    except TypeError:
        worker.work(burst=burst)


def start_n_workers(n: int, queue_names: list):
    """Spawn n worker subprocesses, each consuming the given queues."""
    if not _rq_available:
        logger.error("Queue backend unavailable — cannot start workers")
        sys.exit(1)

    processes = []
    for i in range(n):
        p = multiprocessing.Process(
            target=_worker_process,
            args=(queue_names,),   # plain strings — picklable on macOS spawn
            name=f"rq-worker-{i}",
            daemon=False,
        )
        p.start()
        processes.append(p)
        logger.info(f"Spawned worker process {p.pid} (worker-{i})")

    logger.info(f"All {n} workers running. Waiting...")

    def _graceful_shutdown(signum, frame):
        logger.info(f"Received signal {signum} — graceful shutdown ({len(processes)} workers)...")
        for p in processes:
            if p.is_alive():
                p.terminate()
        # Give workers up to 30s to finish in-flight jobs before forcing kill
        import time as _time
        deadline = _time.time() + 30
        for p in processes:
            remaining = max(0, deadline - _time.time())
            p.join(timeout=remaining)
        for p in processes:
            if p.is_alive():
                logger.warning(f"Worker {p.pid} did not exit — sending SIGKILL")
                p.kill()
        logger.info("All workers stopped.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT,  _graceful_shutdown)

    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        _graceful_shutdown(signal.SIGINT, None)


def _run_kafka_consumer():
    """Start the Kafka consumer in a subprocess."""
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "kafka_consumer.py")],
        stdout=sys.stdout, stderr=sys.stderr,
    )
    logger.info(f"Kafka consumer started (pid={proc.pid})")
    proc.wait()
    logger.warning("Kafka consumer exited — will restart in 10s")
    _time.sleep(10)


def _cowork_scheduler_thread(stop_event: threading.Event):
    """Fire due Cowork scheduled tasks (table `cowork_scheduled_tasks`) on a poll
    loop. Calls workers.cowork_scheduler._fire_due_tasks() — which atomically claims
    due rows (FOR UPDATE SKIP LOCKED) and enqueues each onto connector_queue, so it's
    safe even if more than one instance runs. Runs as ONE daemon thread in the parent
    process (not per worker subprocess). This is what makes /schedule actually fire."""
    try:
        from workers.cowork_scheduler import _fire_due_tasks, _POLL_SECONDS
    except Exception as e:
        logger.error(f"cowork_scheduler thread: import failed → {e}")
        return
    logger.info(f"Cowork scheduler thread started (tick every {_POLL_SECONDS}s)")
    while not stop_event.is_set():
        try:
            n = _fire_due_tasks()
            if n:
                logger.info(f"cowork_scheduler: fired {n} due task(s)")
        except Exception as e:
            logger.error(f"cowork_scheduler thread tick error: {e}")
        stop_event.wait(_POLL_SECONDS)

def _start_cowork_scheduler(stop_event: threading.Event):
    """Start the single daemon thread that fires due Cowork /schedule tasks.

    Without this thread, newly-created tasks keep next_run = NULL forever and
    NEVER fire. Safe to run alongside multiple instances (rows are claimed
    FOR UPDATE SKIP LOCKED). The thread logs its own startup line."""
    threading.Thread(
        target=_cowork_scheduler_thread,
        args=(stop_event,),
        daemon=True,
        name="cowork-scheduler",
    ).start()

def _run_coach_consumer():
    """Start the AiNxt Coach Kafka consumer in a subprocess.

    Only meaningful in prod (COACH_DIRECT_INGEST=false). In dev the gateway
    ingests synchronously, so running this would double-consume — callers
    gate it on COACH_DIRECT_INGEST being false.

    The subprocess itself (workers/coach_consumer.py) has an internal retry
    loop for broker connection failures, so we do not need to respawn it here.
    PM2 / start_all.sh supervise the parent worker process."""
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "coach_consumer.py")],
        stdout=sys.stdout, stderr=sys.stderr,
    )
    logger.info(f"Coach consumer started (pid={proc.pid})")


def _cron_scheduler_thread(stop_event: threading.Event):
    """
    Lightweight cron scheduler running in a background thread.
    Triggers:
      - Thread purge        : daily at 03:00 local time (PLATFORM_TIMEZONE)
      - AD sync             : daily at 02:00 local time
      - Governance SLA check: daily at 09:30 local time — sends reminders
                              for PENDING_APPROVAL items older than 5 days
      - Combined purge      : daily at 00:00 local time — deletes
                              expired generated_documents files + DB rows + chat markers,
                              expired generated_images files + DB rows,
                              and expired uploaded chat files (chat_attachments)
                              stored bytes + DB rows using their existing retain-day env vars
      - Partition maintenance: monthly on the 1st at 02:30 local time
                               creates future partitions, drops expired ones, ANALYZE recent
      - M2 cache cleanup    : weekly on Sunday at 02:00 IST — prunes the
                              content-addressed Maven dep cache to the N newest
                              pom-hash dirs per repo
      - HITL watchdog       : every 15 minutes — expire AWAITING_* runs past 48h TTL

    Note: HOD department digests and Manager team-usage digests are both
    admin-trigger only — they are dispatched via their respective
    /admin/send-hod-statements and /admin/send-manager-statements endpoints.

    All local times are interpreted in PLATFORM_TIMEZONE (default: UTC).
    Set PLATFORM_TIMEZONE (e.g. Asia/Kolkata) to preserve local-time schedules.
    """
    import datetime
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        # Python < 3.9 fallback
        try:
            from backports.zoneinfo import ZoneInfo  # type: ignore
        except ImportError:
            ZoneInfo = None  # type: ignore

    from core.config import PLATFORM_TIMEZONE as _TZ_NAME

    def _get_tz():
        """Return a ZoneInfo object for PLATFORM_TIMEZONE, or None (UTC fallback)."""
        if ZoneInfo is None:
            return None
        try:
            return ZoneInfo(_TZ_NAME)
        except Exception:
            logger.warning(
                f"Cron: invalid PLATFORM_TIMEZONE={_TZ_NAME!r} — falling back to UTC"
            )
            return None

    def _next_utc(hour_local: int, minute_local: int) -> datetime.datetime:
        """Return next UTC datetime for the given local hour:minute (daily cadence).
        'Local' means PLATFORM_TIMEZONE. Falls back to treating the hour as UTC
        if zoneinfo is unavailable or the timezone is invalid.
        """
        tz = _get_tz()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if tz is None:
            # UTC fallback — treat hour_local as UTC directly
            candidate = now_utc.replace(
                hour=hour_local, minute=minute_local, second=0, microsecond=0
            )
            if candidate <= now_utc:
                candidate += datetime.timedelta(days=1)
            return candidate.replace(tzinfo=None)

        now_local = now_utc.astimezone(tz)
        candidate_local = now_local.replace(
            hour=hour_local, minute=minute_local, second=0, microsecond=0
        )
        if candidate_local <= now_local:
            candidate_local += datetime.timedelta(days=1)
        return candidate_local.astimezone(datetime.timezone.utc).replace(tzinfo=None)

    def _next_monthly_utc(day_of_month: int, hour_local: int, minute_local: int) -> datetime.datetime:
        """Return next UTC datetime for a monthly job on day_of_month at the
        given local time. If that slot has already passed this month, advance to next month.
        """
        tz = _get_tz()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_local = now_utc.astimezone(tz) if tz else now_utc

        try:
            candidate_local = now_local.replace(
                day=day_of_month, hour=hour_local, minute=minute_local, second=0, microsecond=0
            )
        except ValueError:
            candidate_local = (now_local.replace(day=1) + datetime.timedelta(days=32)).replace(
                day=day_of_month, hour=hour_local, minute=minute_local, second=0, microsecond=0
            )

        if candidate_local <= now_local:
            next_month = candidate_local.replace(day=1) + datetime.timedelta(days=32)
            try:
                candidate_local = next_month.replace(
                    day=day_of_month, hour=hour_local, minute=minute_local, second=0, microsecond=0
                )
            except ValueError:
                candidate_local = (next_month.replace(day=1) + datetime.timedelta(days=32)).replace(
                    day=day_of_month, hour=hour_local, minute=minute_local, second=0, microsecond=0
                )

        if tz:
            return candidate_local.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return candidate_local.replace(tzinfo=None)

    def _next_weekly_utc(weekday: int, hour_local: int, minute_local: int) -> datetime.datetime:
        """Return next UTC datetime for a weekly job on weekday (0=Mon..6=Sun)
        at the given local time. Advances a week if the slot already passed.
        """
        tz = _get_tz()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_local = now_utc.astimezone(tz) if tz else now_utc
        days_ahead = (weekday - now_local.weekday()) % 7
        candidate_local = (now_local + datetime.timedelta(days=days_ahead)).replace(
            hour=hour_local, minute=minute_local, second=0, microsecond=0
        )
        if candidate_local <= now_local:
            candidate_local += datetime.timedelta(days=7)
        if tz:
            return candidate_local.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return candidate_local.replace(tzinfo=None)

    def _run_coach_weekly_digest():
        """Invoke the Coach weekly digest worker. Gated internally by
        COACH_WEEKLY_MAIL_ENABLED / ENABLE_COACH."""
        try:
            from workers.coach_weekly_mail_worker import run_weekly_digest
            run_weekly_digest()
        except Exception as e:
            logger.error(f"Cron: coach_weekly_digest failed — {e}")

    def _run_partition_maintenance():
        """Invoke partition_maintenance main() directly (no importlib indirection needed)."""
        import subprocess
        import os as _os
        script = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "scripts", "partition_maintenance.py",
        )
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.error(f"Cron: partition_maintenance stderr: {result.stderr[-2000:]}")
            raise RuntimeError(f"partition_maintenance exited {result.returncode}")
        logger.info(f"Cron: partition_maintenance stdout: {result.stdout[-2000:]}")

    def _run_m2_cache_cleanup():
        """Prune the content-addressed Maven dependency cache
        (scripts/cleanup_m2_cache.py — keeps the N newest pom-hash dirs per repo).

        The script documents a weekly cron entry that is not installed on every
        host, so the cache grew unbounded. Running it from the worker's own
        scheduler makes the retention self-enforcing wherever the workers run.
        Subprocess (not import) so a partial rmtree can never take the scheduler
        thread down with it, and so it stays a plain CLI script.
        """
        import subprocess
        import os as _os
        script = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "scripts", "cleanup_m2_cache.py",
        )
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0:
            logger.error(f"Cron: cleanup_m2_cache stderr: {result.stderr[-2000:]}")
            raise RuntimeError(f"cleanup_m2_cache exited {result.returncode}")
        logger.info(f"Cron: cleanup_m2_cache stdout: {result.stdout[-2000:]}")

    # ── Daily jobs: (name, hour_local, minute_local, module, function) ──────
    # Times are in PLATFORM_TIMEZONE (default UTC).
    from core.config import LDAP_SYNC_HOUR as _LDAP_SYNC_HOUR
    jobs = [
        ("thread_purge",        3,  0,  "workers.thread_purge",         "run_purge"),
        ("ad_sync",             _LDAP_SYNC_HOUR, 0, "workers.ad_sync",   "run_ad_sync"),
        ("governance_sla",      9, 30,  "routers.governance_router",    "check_governance_sla_reminders"),
        # Combined retention sweep at 00:00 local time.
        ("purge_worker",        0,  0,  "workers.purge_worker",         "run_purge"),
    ]

    # ── Interval jobs: (name, interval_seconds, module, function) ────────────
    interval_jobs = [
        ("hitl_watchdog",        15 * 60, "workers.sdlc_worker",       "expire_stale_hitl_runs"),
        # Recover documents stuck in INDEXING due to worker crash / server restart.
        # Runs every 10 min — resets stale INDEXING docs (>35 min) to PENDING_APPROVAL
        # with a parse_error message so the approver can retry from the UI.
        ("kb_stale_recovery",    10 * 60, "workers.kb_cleanup_worker",  "recover_stale_indexing_docs"),
        # Recover codewiki_doc_jobs stuck in 'running' whose worker process
        # was killed/crashed outright (no RQ timeout on this queue -- a
        # generation can legitimately run up to ~2 days -- so this checks
        # RQ's own job registry instead of a fixed staleness threshold; see
        # workers/codewiki_worker.py's recover_orphaned_codewiki_jobs()).
        ("codewiki_stale_recovery", 10 * 60, "workers.codewiki_worker",  "recover_orphaned_codewiki_jobs"),
        # P5: Memory quality maintenance — expire stale entries + decay importance scores
        ("memory_maintenance",    6 * 3600, "workers.memory_maintenance_worker", "run_memory_maintenance"),
        # P6: Feedback loop — extract preferences + compute chunk quality penalties
        ("feedback_loop",         1 * 3600, "workers.feedback_loop_worker",      "run_feedback_loop"),
        # P11: Scheduled workflow dispatcher — fires cron-based workflows
        ("workflow_scheduler",    60,       "workers.workflow_scheduler_worker",  "dispatch_scheduled_workflows"),
        # AB Studio user-defined triggers dispatcher — polls `triggers` table
        # and enqueues fires onto RQ. Replaces the in-process APScheduler that
        # previously ran inside every gunicorn worker (multi-worker duplicate
        # fires). Only one instance of this worker runs, so exactly-once.
        ("trigger_dispatcher",    60,       "workers.workflow_scheduler_worker",  "dispatch_due_triggers"),
    ]

    # ── Debounced hierarchy_table rebuild — OFF by default ───────────────────
    # Login-time department/manager staleness checks (services/user_directory_sync.py)
    # flag a Redis dirty key; this job polls that flag every 2 minutes and performs
    # at most one full rebuild per tick. Gated behind HIERARCHY_REBUILD_ENABLED
    # (default false) — when disabled the job is not registered at all and the
    # startup dirty flag below is not set, so no rebuild ever runs on a timer.
    # Refresh manually instead: `python workers/hierarchy_rebuild_worker.py --force`.
    from core.config import HIERARCHY_REBUILD_ENABLED as _HIER_REBUILD_ENABLED

    if _HIER_REBUILD_ENABLED:
        interval_jobs.append(
            ("hierarchy_rebuild", 2 * 60, "workers.hierarchy_rebuild_worker", "rebuild_hierarchy_table_if_dirty")
        )

        # On startup, flag hierarchy_table as dirty so the rebuild worker performs
        # an initial build on its first tick (15 s after startup). This ensures
        # hierarchy_table is populated even on a fresh deployment where no user has
        # logged in yet (the dirty flag is only set on login-time changes otherwise,
        # so the table would stay empty until the first qualifying login).
        try:
            from core.config import RDB_CACHE
            from core.kv import get_kv
            get_kv(RDB_CACHE, decode_responses=True).set("hierarchy_table:dirty", "1")
            logger.info("start_workers: flagged hierarchy_table dirty for initial rebuild on startup.")
        except Exception as _e:
            logger.warning("start_workers: could not flag hierarchy_table dirty on startup: %s", _e)
    else:
        logger.info(
            "start_workers: hierarchy_rebuild job DISABLED (HIERARCHY_REBUILD_ENABLED=false) — "
            "hierarchy_table will not be rebuilt on a schedule."
        )

    # Build initial next-run times for daily jobs
    schedule = {name: _next_utc(h, m) for name, h, m, _, _ in jobs}
    job_map  = {name: (mod, fn) for name, _, _, mod, fn in jobs}

    # Interval jobs: first run 15s after startup, then every interval_seconds
    for iname, isecs, imod, ifn in interval_jobs:
        schedule[iname] = datetime.datetime.utcnow() + datetime.timedelta(seconds=15)
        job_map[iname]  = (imod, ifn)
    interval_map = {iname: isecs for iname, isecs, _, _ in interval_jobs}

    # ── Monthly job: partition maintenance — 1st of each month at 02:30 local time ──
    _PARTITION_MAINT = "partition_maintenance"
    schedule[_PARTITION_MAINT] = _next_monthly_utc(day_of_month=1, hour_local=2, minute_local=30)
    # Store sentinel values so the daily reschedule loop doesn't choke on it
    job_map[_PARTITION_MAINT] = (None, None)

    # ── Weekly job: AiNxt Coach digest — configurable weekday/time (IST) ──────
    from core.config import (
        ENABLE_COACH as _ENABLE_COACH,
        COACH_WEEKLY_MAIL_WEEKDAY as _CW_WD,
        COACH_WEEKLY_MAIL_HOUR_IST as _CW_H,
        COACH_WEEKLY_MAIL_MIN_IST as _CW_M,
    )
    _COACH_WEEKLY = "coach_weekly_digest"
    if _ENABLE_COACH:
        schedule[_COACH_WEEKLY] = _next_weekly_utc(_CW_WD, _CW_H, _CW_M)
        # Note: _CW_H and _CW_M are also interpreted in PLATFORM_TIMEZONE
        job_map[_COACH_WEEKLY] = (None, None)  # sentinel — dispatched explicitly

    # ── Weekly job: Maven dep-cache pruning — Sunday 02:00 IST ────────────────
    # Matches the cadence documented in scripts/cleanup_m2_cache.py, which until
    # now relied on a hand-installed crontab entry.
    _M2_CLEANUP = "m2_cache_cleanup"
    _M2_CLEANUP_WEEKDAY = 6   # Sunday (0=Mon)
    schedule[_M2_CLEANUP] = _next_weekly_utc(_M2_CLEANUP_WEEKDAY, 2, 0)
    job_map[_M2_CLEANUP] = (None, None)  # sentinel — dispatched explicitly

    logger.info(f"Cron scheduler started. Schedule: { {k: str(v) for k, v in schedule.items()} }")

    while not stop_event.is_set():
        now = datetime.datetime.utcnow()
        for name, next_run in list(schedule.items()):
            if now >= next_run:
                logger.info(f"Cron: running {name}")
                try:
                    if name == _PARTITION_MAINT:
                        _run_partition_maintenance()
                    elif name == _COACH_WEEKLY:
                        _run_coach_weekly_digest()
                    elif name == _M2_CLEANUP:
                        _run_m2_cache_cleanup()
                    else:
                        mod_name, fn_name = job_map[name]
                        import importlib
                        mod = importlib.import_module(mod_name)
                        fn  = getattr(mod, fn_name)
                        fn()
                    logger.info(f"Cron: {name} completed")
                except Exception as e:
                    logger.error(f"Cron: {name} failed — {e}")

                # Reschedule
                if name == _PARTITION_MAINT:
                    schedule[name] = _next_monthly_utc(day_of_month=1, hour_local=2, minute_local=30)
                elif name == _COACH_WEEKLY:
                    schedule[name] = _next_weekly_utc(_CW_WD, _CW_H, _CW_M)
                elif name == _M2_CLEANUP:
                    schedule[name] = _next_weekly_utc(_M2_CLEANUP_WEEKDAY, 2, 0)
                elif name in interval_map:
                    schedule[name] = datetime.datetime.utcnow() + datetime.timedelta(seconds=interval_map[name])
                else:
                    h, m = next((h, m) for n, h, m, _, _ in jobs if n == name)
                    schedule[name] = _next_utc(h, m)

        stop_event.wait(60)  # check every 60 seconds


# ============================================================
# BUDGET MONTHLY RESET CRON
#
# Fires according to BUDGET_MONTHLY_RESET_CRON (UTC cron expression,
# default "15 3 1 * *" = 03:15 UTC on the 1st of each month) and invokes
# services.budget_audit_service.snapshot_and_reset_all_budgeted_users for
# the *closing* month (the previous calendar month at fire time).
#
# Gated end-to-end by BUDGET_MONTHLY_RESET_ENABLED (default: false).
# When the flag is false, this thread is NOT started by main(); the
# function itself ALSO re-checks the flag at fire time as defense-in-depth.
# ============================================================

def _budget_monthly_reset_enabled() -> bool:
    """Mirror of services.hod_budget_governor._enforcement_enabled() pattern."""
    return os.getenv("BUDGET_MONTHLY_RESET_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _budget_reset_warning_cron_thread():
    """
    Sends pre-reset warning emails for the CURRENT (about-to-close) calendar
    month, ~24h before the actual reset.

    Schedule: BUDGET_MONTHLY_RESET_WARNING_CRON (UTC).
              Default '15 3 28-31 * *' — fires daily at 03:15 UTC on the
              28th-31st of the month, but the inner guard only actually
              sends if today is the LAST day of the month. This avoids
              depending on croniter 'L' support.

    Set the env var to "" to disable warning emails without disabling resets.
    Re-checks BUDGET_MONTHLY_RESET_ENABLED on every fire.
    Best-effort in-memory dedup so two fires in the same period don't
    double-send.
    """
    from datetime import datetime, timedelta, timezone
    from croniter import croniter

    cron_expr = os.getenv(
        "BUDGET_MONTHLY_RESET_WARNING_CRON", "15 3 28-31 * *"
    ).strip()
    if not cron_expr:
        logger.info(
            "budget_monthly_reset_warning: disabled via empty cron expression"
        )
        return

    already_warned: set = set()
    logger.info(
        "budget_monthly_reset_warning: cron thread loop entered (cron=%s, UTC)",
        cron_expr,
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            itr = croniter(cron_expr, now)
            next_fire = itr.get_next(datetime)
            sleep_s = max(1.0, (next_fire - now).total_seconds())
            logger.info(
                "budget_monthly_reset_warning: next fire at %s UTC (sleep=%.0fs)",
                next_fire.isoformat(), sleep_s,
            )
            _time.sleep(sleep_s)

            if not _budget_monthly_reset_enabled():
                logger.info(
                    "budget_monthly_reset_warning: flag off; skipping this fire"
                )
                continue

            fire_time = datetime.now(timezone.utc)
            # Only run if today is the LAST day of the month — guards
            # against the 28-31 broad cron firing on the 28th of February
            # in a non-leap year, for instance.
            tomorrow = fire_time + timedelta(days=1)
            is_last_day = (tomorrow.day == 1)
            if not is_last_day:
                logger.info(
                    "budget_monthly_reset_warning: not last-day-of-month (today=%s); skipping",
                    fire_time.date().isoformat(),
                )
                continue

            period = fire_time.strftime("%Y-%m")
            if period in already_warned:
                logger.info(
                    "budget_monthly_reset_warning: already warned for %s; skipping",
                    period,
                )
                continue

            from services.budget_audit_service import (
                send_pre_reset_warnings_for_all,
            )
            result = send_pre_reset_warnings_for_all(period)
            already_warned.add(period)
            logger.info("budget_monthly_reset_warning: completed %s", result)
        except Exception:
            logger.exception(
                "budget_monthly_reset_warning: iteration failed; sleeping 60s"
            )
            _time.sleep(60)


def _budget_reset_cron_thread():
    """
    Runs services.budget_audit_service.snapshot_and_reset_all_budgeted_users
    on the schedule defined by BUDGET_MONTHLY_RESET_CRON (UTC).

    Computes period_yyyymm as the previous calendar month at fire time
    (i.e. the month that just closed).

    Re-checks BUDGET_MONTHLY_RESET_ENABLED on every fire (defense-in-depth).
    """
    from datetime import datetime, timedelta, timezone
    from croniter import croniter

    cron_expr = os.getenv("BUDGET_MONTHLY_RESET_CRON", "15 3 1 * *")
    logger.info(
        "budget_monthly_reset: cron thread loop entered (cron=%s, UTC)",
        cron_expr,
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            itr = croniter(cron_expr, now)
            next_fire = itr.get_next(datetime)
            sleep_s = max(1.0, (next_fire - now).total_seconds())
            logger.info(
                "budget_monthly_reset: next fire at %s UTC (cron=%s, sleep=%.0fs)",
                next_fire.isoformat(), cron_expr, sleep_s,
            )
            _time.sleep(sleep_s)

            # Defense-in-depth: re-check the master flag at fire time.
            if not _budget_monthly_reset_enabled():
                logger.info(
                    "budget_monthly_reset: flag turned off mid-run; skipping this fire"
                )
                continue

            fire_time = datetime.now(timezone.utc)
            first_of_this_month = fire_time.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            closing = first_of_this_month - timedelta(days=1)
            period = closing.strftime("%Y-%m")

            from services.budget_audit_service import (
                snapshot_and_reset_all_budgeted_users,
            )
            result = snapshot_and_reset_all_budgeted_users(period)
            logger.info("budget_monthly_reset: completed %s", result)
        except Exception:
            logger.exception(
                "budget_monthly_reset: iteration failed; sleeping 60s"
            )
            _time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="Start AiNxt rq workers")
    parser.add_argument("--sdlc",      action="store_true", help="SDLC-only worker (sdlc_queue)")
    parser.add_argument("--chat",      action="store_true", help="Chat workers (high + default + chat_queue)")
    parser.add_argument("--index",     action="store_true", help="Index workers (index_queue)")
    parser.add_argument("--agent",     action="store_true", help="Agent workers (agent_queue)")
    parser.add_argument("--kb",        action="store_true", help="KB ingest workers (kb_queue)")
    parser.add_argument("--security",  action="store_true", help="Security scan workers (security_queue)")
    parser.add_argument("--doc",       action="store_true", help="Document generation workers (doc_queue)")
    parser.add_argument("--codewiki",  action="store_true", help="CodeWiki documentation-generation workers (codewiki_queue)")
    parser.add_argument("--kafka",     action="store_true", help="Start Kafka consumer subprocess")
    parser.add_argument("--connector", action="store_true", help="Connector-queue workers (connector_queue) — async connector tool calls + fired Buddy/Cowork scheduled tasks")
    parser.add_argument("--coach",     action="store_true", help="Coach workers (coach_queue) + coach Kafka consumer")
    parser.add_argument("--cowork-scheduler", dest="cowork_scheduler", action="store_true",
                        help="Fire due Cowork /schedule tasks (auto-on in default all-queues mode)")
    parser.add_argument("--scheduler", action="store_true", help="Start background cron scheduler (thread_purge + ad_sync)")
    parser.add_argument("--n", type=int, default=None,
                        help="Number of worker processes to spawn. "
                             "Default: SDLC_WORKER_COUNT env for --sdlc, else 1.")
    parser.add_argument("--burst", action="store_true",
                        help="Burst mode — exit when queue is empty")
    args = parser.parse_args()

    # --- Optional background threads ---------------------------------
    stop_event = threading.Event()

    if args.scheduler:
        sched_thread = threading.Thread(
            target=_cron_scheduler_thread,
            args=(stop_event,),
            daemon=True,
            name="cron-scheduler",
        )
        sched_thread.start()
        logger.info("Background cron scheduler thread started")

        # P11: Resume interrupted workflows on startup
        try:
            from workers.durable_workflow_worker import resume_interrupted_workflows
            _resumed = resume_interrupted_workflows()
            if _resumed:
                logger.info(f"P11: re-enqueued {_resumed} interrupted workflow(s) on startup")
        except Exception as _rwf_e:
            logger.warning(f"P11: resume_interrupted_workflows failed (non-fatal): {_rwf_e}")

        # ── Budget monthly reset cron — gated by master flag ────────────
        if _budget_monthly_reset_enabled():
            budget_reset_thread = threading.Thread(
                target=_budget_reset_cron_thread,
                daemon=True,
                name="budget-reset-cron",
            )
            budget_reset_thread.start()
            logger.info(
                "budget_monthly_reset: cron thread started (cron=%s)",
                os.getenv("BUDGET_MONTHLY_RESET_CRON", "15 3 1 * *"),
            )

            # Pre-reset warning cron — same master flag.
            warning_thread = threading.Thread(
                target=_budget_reset_warning_cron_thread,
                daemon=True,
                name="budget-reset-warning-cron",
            )
            warning_thread.start()
            logger.info(
                "budget_monthly_reset_warning: cron thread started (cron=%s)",
                os.getenv("BUDGET_MONTHLY_RESET_WARNING_CRON", "15 3 28-31 * *"),
            )
        else:
            logger.info(
                "budget_monthly_reset: disabled via BUDGET_MONTHLY_RESET_ENABLED"
            )

        # ── Preference learner (feedback → per-user style pref, loop C) ──
        # Background derivation of durable style preferences from thumbs
        # feedback. One daemon thread in the parent process; gated by
        # PREFERENCE_LEARNING (default on). Never touches the request path.
        try:
            from workers.preference_learner import (
                preference_learner_thread as _pref_thread,
                _ENABLED as _pref_enabled,
            )
            if _pref_enabled:
                pref_learner_thread = threading.Thread(
                    target=_pref_thread,
                    args=(stop_event,),
                    daemon=True,
                    name="preference-learner",
                )
                pref_learner_thread.start()
                logger.info("preference_learner: background thread started")
            else:
                logger.info("preference_learner: disabled via PREFERENCE_LEARNING")
        except Exception as _pl_e:
            logger.warning(f"preference_learner: thread start failed (non-fatal): {_pl_e}")

    if args.kafka:
        kafka_thread = threading.Thread(
            target=_run_kafka_consumer,
            daemon=True,
            name="kafka-consumer",
        )
        kafka_thread.start()
        logger.info("Kafka consumer thread started")
    
    # Coach Kafka consumer — only when Coach is on AND direct-ingest is off
    # (prod). In dev (COACH_DIRECT_INGEST=true) the gateway ingests inline, so
    # starting this would double-consume the topic.
    if args.coach:
        try:
            from core.config import ENABLE_COACH as _EC, COACH_DIRECT_INGEST as _CDI
        except Exception:
            _EC, _CDI = False, True
        if _EC and not _CDI:
            coach_thread = threading.Thread(
                target=_run_coach_consumer,
                daemon=True,
                name="coach-consumer",
            )
            coach_thread.start()
            logger.info("Coach consumer thread started")
        else:
            logger.info("Coach consumer not started (ENABLE_COACH off or COACH_DIRECT_INGEST on)")
    # -----------------------------------------------------------------

    if args.sdlc:
        queue_names = [Q_SDLC]
    elif args.chat:
        queue_names = [Q_HIGH, Q_DEFAULT, Q_CHAT]
    elif args.index:
        queue_names = [Q_INDEX]
    elif args.agent:
        queue_names = [Q_AGENT]
    elif args.kb:
        queue_names = [Q_KB]
    elif args.security:
        queue_names = [Q_SECURITY]
    elif args.doc:
        queue_names = [Q_DOC]
    elif args.codewiki:
        queue_names = [Q_CODEWIKI]
    elif args.connector:
        queue_names = [Q_CONNECTOR]
    elif args.coach:
        queue_names = [Q_COACH]
    elif args.kafka or args.scheduler or args.cowork_scheduler:
        # Scheduler/Kafka-only mode: no rq workers, just keep process alive.
        # In this mode the cowork scheduler thread is what actually fires due
        # /schedule tasks, so start it before parking.
        if args.cowork_scheduler:
            _start_cowork_scheduler(stop_event)
        logger.info("Running scheduler/kafka-only mode (no rq workers). Press Ctrl+C to stop.")
        try:
            while True:
                _time.sleep(30)
        except KeyboardInterrupt:
            stop_event.set()
            logger.info("Scheduler stopped.")
        return
    else:
        queue_names = ALL_QUEUES  # all queues, priority order

    # Resolve worker-process count: an explicit --n always wins; otherwise the
    # sdlc pool defaults from SDLC_WORKER_COUNT so the whole capacity profile can
    # live in one env file. All other pools keep their historical default of 1.
    if args.n is not None:
        _n = args.n
    elif args.sdlc:
        _n = _env_int("SDLC_WORKER_COUNT", 1)
    else:
        _n = 1
    # Fire due Cowork /schedule tasks whenever this process serves
    # connector_queue (default all-queues mode OR --connector), or on explicit
    # --cowork-scheduler request.
    if args.cowork_scheduler or queue_names is ALL_QUEUES or Q_CONNECTOR in queue_names:
        _start_cowork_scheduler(stop_event)

    logger.info(f"Spawning {_n} worker process(es) for queues={queue_names}")

    if _n > 1:
        start_n_workers(_n, queue_names)
    else:
        start_worker(queue_names, burst=args.burst)

    stop_event.set()


if __name__ == "__main__":
    main()
