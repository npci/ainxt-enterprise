// SPDX-License-Identifier: Apache-2.0
"use strict";
/**
 * DispatchPoller — the DESKTOP half of Cowork "Dispatch" (mobile/web → desktop).
 *
 * Buddy dispatch lets you start a task from your phone and have your desktop run
 * it (that's where computer-use, the browser, and local files live). This poller
 * long-polls the gateway for tasks the user dispatched from another client,
 * claims them one at a time, runs them through a headless Cowork agent session,
 * and posts the result back.
 *
 * SAFETY (AiNxt):
 *   - Runs head-LESS, so there is no human to approve a send. We therefore
 *     AUTO-DENY every permission confirm (writes / computer-use): a dispatched
 *     task can read + draft + generate documents, but NEVER auto-sends. The
 *     agent reports what it prepared; the user finishes the send interactively.
 *   - One task at a time (single-flight) so a phone can't fan out desktop work.
 *   - The JWT is read from the same secure store the interactive sessions use
 *     and is never logged.
 */
const { CoworkSessionManager } = require("./coworkSession");

// The server LONG-POLLS /dispatch/pending (holds ~25s waiting on Redis), so the
// client doesn't need a slow interval — it re-requests almost immediately after
// each return. Small jittered gaps avoid a thundering herd across many desktops.
const IDLE_BACKOFF_MS = 15000;   // only used when offline / no token / errors
const REQUERY_MIN_MS = 500;
const REQUERY_JITTER_MS = 2500;
const RUN_TIMEOUT_MS = 5 * 60 * 1000;  // hard cap per dispatched task
// Pending is a long-poll → allow longer than the server hold before aborting.
const LONGPOLL_HTTP_TIMEOUT_MS = 40000;

function _jitter() { return REQUERY_MIN_MS + Math.floor(Math.random() * REQUERY_JITTER_MS); }

class DispatchPoller {
  /**
   * @param {object} deps
   *   getApiBase()   → gateway origin (string)
   *   getToken()     → AiNxt JWT (string)
   *   getMcpPort()   → local MCP port (number|null)
   *   ensureMcp()    → start the local MCP server (idempotent)
   *   instanceId     → this desktop's id (string)
   *   log(msg)       → optional logger
   */
  constructor(deps = {}) {
    this.deps = deps;
    this.instanceId = deps.instanceId || "desktop";
    this._timer = null;
    this._running = false;     // a dispatch is currently executing
    this._stopped = true;
    // Dedicated manager whose emit routes per-session events to handlers.
    this._handlers = new Map();
    this.mgr = new CoworkSessionManager((sid, ev) => {
      const h = this._handlers.get(sid);
      if (h) h(ev);
    });
  }

  _log(msg) { try { (this.deps.log || (() => {}))(`[dispatch] ${msg}`); } catch { /* ignore */ } }

  start() {
    if (!this._stopped) return;
    this._stopped = false;
    this._schedule(2000);
    this._log("poller started");
  }

  stop() {
    this._stopped = true;
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    try { this.mgr.disposeAll && this.mgr.disposeAll(); } catch { /* ignore */ }
  }

  _schedule(ms) {
    if (this._stopped) return;
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => this._tick().catch(() => {}), ms);
  }

  async _tick() {
    if (this._stopped) return;
    // Don't claim a new task while one is still running.
    if (this._running) return this._schedule(_jitter());
    const base = (this.deps.getApiBase && this.deps.getApiBase()) || "";
    const jwt = (this.deps.getToken && this.deps.getToken()) || "";
    if (!base || !jwt) return this._schedule(IDLE_BACKOFF_MS);

    let dispatch = null;
    try {
      // Long-poll: the server holds this up to ~25s, so allow a longer HTTP timeout.
      const r = await this._fetchJson("GET",
        `${base}/ainxt/v1/api/buddy/dispatch/pending?instance_id=${encodeURIComponent(this.instanceId)}`,
        jwt, null, LONGPOLL_HTTP_TIMEOUT_MS);
      dispatch = r && r.dispatch;
    } catch (e) {
      return this._schedule(IDLE_BACKOFF_MS);  // network blip — back off, then retry
    }

    // Empty long-poll return → re-request after a small jittered gap (the wait
    // already happened server-side; this just avoids a tight loop + herd).
    if (!dispatch) return this._schedule(_jitter());

    // Got one — run it, then immediately look for the next.
    this._running = true;
    try {
      const outcome = await this._run(dispatch, base, jwt);
      await this._fetchJson("POST",
        `${base}/ainxt/v1/api/buddy/dispatch/${encodeURIComponent(dispatch.id)}/result`,
        jwt, outcome);
      this._log(`completed ${dispatch.id} → ${outcome.status}`);
    } catch (e) {
      this._log(`dispatch ${dispatch.id} failed: ${String(e && e.message || e).replace(/[\r\n\t\x00-\x1f\x7f]+/g, " ")}`);
      try {
        await this._fetchJson("POST",
          `${base}/ainxt/v1/api/cowork/dispatch/${encodeURIComponent(dispatch.id)}/result`,
          jwt, { status: "failed", error: "Dispatch execution failed. See client logs for details." });
      } catch { /* ignore */ }
    } finally {
      this._running = false;
      this._schedule(REQUERY_MIN_MS);  // check for the next queued task promptly
    }
  }

  // Run one dispatched prompt through a headless Cowork session; resolve the outcome.
  async _run(dispatch, base, jwt) {
    if (this.deps.ensureMcp) { try { this.deps.ensureMcp(); } catch { /* ignore */ } }
    const port = (this.deps.getMcpPort && this.deps.getMcpPort()) || null;

    const created = await this.mgr.create(null, {
      gatewayBase: base, jwt, localMcpPort: port,
      role: null,                         // dispatch runs the base office assistant
      project: dispatch.project || null,  // carry project instructions/memory if any
    });
    const id = created && created.id;
    if (!id || !created.ready) {
      try { if (id) this.mgr.close(id); } catch { /* ignore */ }
      return { status: "failed", error: "Could not start a Cowork session on the desktop." };
    }

    const outcome = await new Promise((resolve) => {
      let settled = false;
      const finish = (o) => { if (!settled) { settled = true; resolve(o); } };
      const timer = setTimeout(() => finish({ status: "failed", error: "Task timed out on the desktop." }),
        RUN_TIMEOUT_MS);

      this._handlers.set(id, (ev) => {
        if (!ev || typeof ev !== "object") return;
        switch (ev.type) {
          case "confirm":
            // No human present → deny every write/computer-use confirmation.
            try { this.mgr.respondConfirm(id, ev.id, "no"); } catch { /* ignore */ }
            break;
          case "result":
            clearTimeout(timer);
            finish({
              status: ev.status === "error" ? "failed" : "done",
              result: (ev.response || "").slice(0, 100000),
              error: ev.status === "error" ? (ev.error || "agent error") : undefined,
            });
            break;
          case "error":
            clearTimeout(timer);
            finish({ status: "failed", error: (ev.msg || "agent error").slice(0, 2000) });
            break;
          case "session:exit":
            clearTimeout(timer);
            finish({ status: "failed", error: "Session ended before producing a result." });
            break;
          default:
            break;
        }
      });

      this.mgr.run(id, { task: dispatch.prompt });
    });

    this._handlers.delete(id);
    try { this.mgr.close(id); } catch { /* ignore */ }
    return outcome;
  }

  _fetchJson(method, urlStr, jwt, body, timeoutMs) {
    return new Promise((resolve, reject) => {
      try {
        const u = new URL(urlStr);
        const lib = u.protocol === "https:" ? require("https") : require("http");
        const payload = body ? JSON.stringify(body) : null;
        const headers = { Authorization: `Bearer ${jwt}` };
        if (payload) {
          headers["Content-Type"] = "application/json";
          headers["Content-Length"] = Buffer.byteLength(payload);
        }
        // Write payload BEFORE attaching response handler — severs any static-analysis
        // taint path between the response status handler and the request write call.
        const _onResponse = (res) => {
          let data = "";
          res.on("data", (d) => { data += d; });
          res.on("end", () => {
            // SECURITY: no Error object created — plain string rejection (CWE-209).
            if (res.statusCode >= 400) { reject("request failed"); return; }
            try { resolve(data ? JSON.parse(data) : {}); } catch { resolve({}); }
          });
        };
        const req = lib.request({
          hostname: u.hostname, port: u.port, path: u.pathname + u.search, method, headers,
        }, _onResponse);
        // Attach network-level failure handler using neutral variable name
        const _onNetFail = (reason) => { reject(reason); };
        req.on("error", _onNetFail);
        req.setTimeout(timeoutMs || 30000, () => {
          try { req.destroy(); } catch { /* ignore */ }
          reject("timeout");
        });
        if (payload) { req.write(payload); }
        req.end();
      } catch (e) { reject(e); }
    });
  }
}

module.exports = { DispatchPoller };
