#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""
Dedicated Discussions-module RQ worker.

Consumes ONLY `discussions_queue` (agent_bridge.run_discussions_bot_job) —
isolated from every other worker pool, own PM2 process, own logs.

Run under PM2 (from the repo root, with the venv python):
    pm2 start venv/bin/python --name ainxt-discussions-worker --cwd /path/to/ai-copilot \
        -i 3 -- -m services.discussions_svc.worker

macOS note (feedback_macos_rq_fork_crash): forking rq.Worker crashes on any
subprocess/docker-touching job via objc SIGABRT — use SimpleWorker on darwin,
same as workers/start_workers.py.
"""
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def _load_env():
    """core.config reads process env only (no dotenv) — load an env file so
    this standalone worker gets the SAME REDIS_*/POSTGRES_*/ANSWER_*/etc. as
    the gateway. Point DISCUSSIONS_ENV_FILE at your env file, else <repo>/.env."""
    path = os.getenv("DISCUSSIONS_ENV_FILE") or os.path.join(ROOT, ".env")
    if not os.path.isfile(path):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
    except Exception:
        for line in open(path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for _n in ("discussions_svc", "rq.worker"):
    logging.getLogger(_n).setLevel(logging.INFO)

log = logging.getLogger("discussions_svc.worker")


def main():
    from services.discussions_svc.config import ENABLE_DISCUSSIONS
    if not ENABLE_DISCUSSIONS:
        log.error("discussions-worker: ENABLE_DISCUSSIONS is false — refusing to start")
        sys.exit(1)

    try:
        import rq
        from core.job_queue import get_queue, Q_DISCUSSIONS
    except Exception:
        log.error("discussions-worker: cannot import rq/job_queue")
        sys.exit(1)

    from core.config import REDIS_HOST, REDIS_PORT
    log.info("discussions-worker: REDIS=%s:%s queue=%s", REDIS_HOST, REDIS_PORT, type(Q_DISCUSSIONS).__name__)
    try:
        q = get_queue(Q_DISCUSSIONS)
        conn = q.connection
        conn.ping()
    except Exception:
        log.error("discussions-worker: cannot reach Redis at %s:%s — set REDIS_HOST/"
                  "REDIS_PORT/REDIS_PASSWORD (via DISCUSSIONS_ENV_FILE or PM2 env).",
                  REDIS_HOST, REDIS_PORT)
        sys.exit(1)

    Worker = rq.SimpleWorker if sys.platform == "darwin" else rq.Worker
    log.info("discussions-worker starting — queue=%s worker=%s", Q_DISCUSSIONS, Worker.__name__)
    Worker([q], connection=conn).work(with_scheduler=False)


if __name__ == "__main__":
    main()
