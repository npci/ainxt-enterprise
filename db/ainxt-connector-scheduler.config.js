// SPDX-License-Identifier: MIT
/**
 * PM2 config for Buddy (Cowork) scheduled tasks + the connector queue.
 *
 * Two SEPARATE apps, on purpose:
 *
 *   1. ainxt-cowork-scheduler  — the poll loop (workers/cowork_scheduler.py).
 *      MUST run as a SINGLE instance. It finds due `cowork_scheduled_tasks`
 *      rows (FOR UPDATE SKIP LOCKED), bootstraps their next_run from cron, and
 *      enqueues each onto `connector_queue`, then advances next_run. Lightweight;
 *      never scale this past 1 (duplicate loops are safe via SKIP LOCKED but
 *      pointless).
 *
 *   2. ainxt-connector-worker  — RQ workers on `connector_queue`. These execute
 *      the fired scheduled tasks (workers.cowork_task_worker.run_scheduled_task)
 *      AND handle interactive async connector calls (email search, transcripts,
 *      calendar via enqueue_connector_job). No other worker pool consumes this
 *      queue, so without this app both features silently do nothing.
 *
 *   3. ainxt-connector-token-refresher — renews OAuth access tokens BEFORE they
 *      expire (workers/connector_token_refresher.py). Without it, refresh is
 *      purely lazy, so a task firing at 21:00 is the first thing to discover a
 *      token that went stale at 10:00 — which surfaced to users as "please
 *      connect the M365 connector". Needs the SAME FERNET_KEY as the gateway and,
 *      for single-tenant Microsoft apps, AZURE_AD_TENANT_ID.
 *
 * Env (DB/Redis/LLM_PROXY_URL/etc.) is read from /opt/ainxt/.env by the app on
 * import — same convention as deploy/ainxt-compression.config.js. Adjust `cwd`
 * and the `--n` worker count for your box.
 *
 * Usage:
 *   pm2 start deploy/ainxt-connector-scheduler.config.js
 *   pm2 logs  ainxt-cowork-scheduler
 *   pm2 logs  ainxt-connector-worker
 *   pm2 save
 */
module.exports = {
  apps: [
    {
      name:         "ainxt-cowork-scheduler",
      script:       "venv/bin/python",
      args:         "workers/cowork_scheduler.py",
      cwd:          "/opt/ainxt",
      interpreter:  "none",
      instances:    1,            // SINGLETON — never scale the poll loop
      autorestart:  true,
      watch:        false,
      restart_delay: 5000,
      max_restarts:  10,
      max_memory_restart: "512M",
      log_file:     "/opt/ainxt/logs/cowork-scheduler.log",
      error_file:   "/opt/ainxt/logs/cowork-scheduler-err.log",
      out_file:     "/opt/ainxt/logs/cowork-scheduler-out.log",
      time:         true,
    },
    {
      name:         "ainxt-connector-worker",
      script:       "venv/bin/python",
      // --n 4 forks 4 RQ workers on connector_queue; the parent PM2 process
      // supervises them (same pattern as the chat/sdlc worker pools).
      args:         "workers/start_workers.py --connector --n 4",
      cwd:          "/opt/ainxt",
      interpreter:  "none",
      instances:    1,            // one supervisor; scale via --n, not PM2 instances
      autorestart:  true,
      watch:        false,
      restart_delay: 5000,
      max_restarts:  10,
      max_memory_restart: "2G",
      log_file:     "/opt/ainxt/logs/connector-worker.log",
      error_file:   "/opt/ainxt/logs/connector-worker-err.log",
      out_file:     "/opt/ainxt/logs/connector-worker-out.log",
      time:         true,
    },
    {
      name:         "ainxt-connector-token-refresher",
      script:       "venv/bin/python",
      args:         "workers/connector_token_refresher.py",
      cwd:          "/opt/ainxt",
      interpreter:  "none",
      instances:    1,            // SINGLETON — refreshing is idempotent anyway
      autorestart:  true,
      watch:        false,
      restart_delay: 5000,
      max_restarts:  10,
      max_memory_restart: "512M",
      log_file:     "/opt/ainxt/logs/connector-token-refresher.log",
      error_file:   "/opt/ainxt/logs/connector-token-refresher-err.log",
      out_file:     "/opt/ainxt/logs/connector-token-refresher-out.log",
      time:         true,
    },
  ],
};
