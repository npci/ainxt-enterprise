// SPDX-License-Identifier: MIT
"use strict";
/**
 * Crash-safe PID registry for spawned ainxt-cli agent processes.
 *
 * Each live CLI pid is recorded to a small JSON file. On a CLEAN exit the manager
 * disposes sessions and removes their pids. But if the Electron main process
 * CRASHES (SIGKILL, power loss), dispose() never runs and the detached CLI
 * processes are orphaned — still billing the gateway. On next startup we read this
 * file and kill any pids that are still alive (best-effort), then reset it.
 *
 * Only pids WE recorded are touched, and only when the process still exists, so an
 * unrelated process that reused the pid is not affected beyond a signal it can
 * ignore. Windows: process-group kill isn't used; we fall back to per-pid kill.
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const FILE = path.join(os.tmpdir(), "ainxt-buddy-pids.json");

function _read() {
  try { return JSON.parse(fs.readFileSync(FILE, "utf-8")) || []; }
  catch { return []; }
}
function _write(arr) {
  try { fs.writeFileSync(FILE, JSON.stringify([...new Set(arr)]), { mode: 0o600 }); }
  catch { /* ignore */ }
}

function record(pid) {
  if (!pid) return;
  const arr = _read();
  if (!arr.includes(pid)) { arr.push(pid); _write(arr); }
}

function remove(pid) {
  if (!pid) return;
  _write(_read().filter((p) => p !== pid));
}

/**
 * Kill any pids left over from a previous (crashed) run. Call ONCE at app startup
 * BEFORE spawning new sessions. Safe: only signals pids that are still alive.
 */
function sweepOrphans() {
  const arr = _read();
  let killed = 0;
  for (const pid of arr) {
    try {
      process.kill(pid, 0);            // probe: throws if the pid is gone
      try {
        if (process.platform !== "win32") process.kill(-pid, "SIGKILL");
        else process.kill(pid);
      } catch { try { process.kill(pid, "SIGKILL"); } catch { /* gone */ } }
      killed++;
    } catch { /* pid not alive — skip */ }
  }
  _write([]);  // reset; freshly spawned sessions re-record themselves
  return killed;
}

module.exports = { record, remove, sweepOrphans, FILE };
