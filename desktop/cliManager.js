// SPDX-License-Identifier: Apache-2.0
"use strict";
/**
 * CliSession — drives the FULL ainxt agent (app/main.tsx) headless over the
 * Claude Agent-SDK stream-json protocol.
 *
 * Spawns: `ainxt --full --print --input-format stream-json --output-format
 * stream-json --verbose --include-partial-messages --tools default` in the chosen
 * folder. cli.tsx's `--full` route normalises argv + adds `--bare` so auth flows
 * through the AiNxt gateway (ANTHROPIC_API_KEY = the AiNxt JWT). One long-lived
 * process = a multi-turn session (the SDK keeps context; we just feed each user
 * turn on stdin — no manual history).
 *
 * Wire format:
 *   stdin  (us → CLI): {type:"user", message:{role:"user", content:"<text>"}}
 *                      {type:"control_response", response:{subtype:"success",
 *                        request_id, response:{behavior:"allow"|"deny"}}}
 *                      {type:"control_request", request_id, request:{subtype:"interrupt"}}
 *   stdout (CLI → us): {type:"system",subtype:"init"} | {type:"stream_event",event} |
 *                      {type:"assistant",message:{content:[text|tool_use]}} |
 *                      {type:"user",message:{content:[tool_result]}} |
 *                      {type:"control_request",request_id,request:{subtype:"can_use_tool",...}} |
 *                      {type:"result",subtype,result,is_error,duration_ms}
 *
 * We translate that to the SAME event vocabulary the Code UI already consumes
 * (token / tool:start / tool:done / confirm / result / notice), so the renderer
 * is unchanged. Permissions use per-action confirm: a can_use_tool request →
 * the Code permission dialog → control_response.
 */
const { spawn } = require("child_process");
const { randomUUID } = require("crypto");
const { resolveCliBinary } = require("./binary");

function toolDetail(input) {
  if (!input || typeof input !== "object") return "";
  const v = input.command || input.file_path || input.path || input.pattern || input.url || "";
  return v ? String(v).slice(0, 80) : "";
}

class CliSession {
  constructor(id, cwd, emit) {
    this.id = id;
    this.cwd = cwd;
    this.emit = emit;
    this.proc = null;
    this.ready = false;
    this.busy = false;
    this._stdoutBuf = "";
    this._readyResolvers = [];
    this._toolNames = new Map();   // tool_use_id → name (for tool:done)
    this._textStreamed = false;    // did partial deltas cover this turn's text?
  }

  start() {
    const bin = resolveCliBinary();
    if (!bin) {
      this.emit(this.id, { type: "error", msg: "AiNxt CLI binary not found. Build it with `bun scripts/build-dist.ts`." });
      this.emit(this.id, { type: "session:exit", code: -1 });
      return Promise.resolve(false);
    }

    const args = [
      ...bin.args,
      "--full",
      "--print",
      "--input-format", "stream-json",
      "--output-format", "stream-json",
      "--verbose",
      "--include-partial-messages",
      "--tools", "default",
    ];
    this.proc = spawn(bin.command, args, {
      cwd: this.cwd || process.cwd(),
      env: { ...process.env, FORCE_COLOR: "0", AINXT_IS_COWORK: "1" },
    });

    this.proc.stdout.setEncoding("utf-8");
    this.proc.stdout.on("data", (chunk) => this._onStdout(chunk));
    this.proc.stderr.on("data", (d) => {
      const text = d.toString().trim();
      if (text) this.emit(this.id, { type: "notice", msg: text, level: "info" });
    });
    this.proc.on("error", (err) => {
      this.emit(this.id, { type: "error", msg: `CLI process error: ${err.message}` });
    });
    this.proc.on("spawn", () => {
      // Ready once the process is up — stdin writes are buffered until the SDK
      // finishes booting and starts reading, so we needn't wait for init.
      this.ready = true;
      this._readyResolvers.forEach((r) => r(true));
      this._readyResolvers = [];
    });
    this.proc.on("close", (code) => {
      this.ready = false;
      this.busy = false;
      this.emit(this.id, { type: "session:exit", code });
      this._readyResolvers.forEach((r) => r(false));
      this._readyResolvers = [];
    });

    return new Promise((resolve) => {
      if (this.ready) return resolve(true);
      this._readyResolvers.push(resolve);
    });
  }

  _onStdout(chunk) {
    this._stdoutBuf += chunk;
    let nl;
    while ((nl = this._stdoutBuf.indexOf("\n")) >= 0) {
      const line = this._stdoutBuf.slice(0, nl).trim();
      this._stdoutBuf = this._stdoutBuf.slice(nl + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); }
      catch { continue; } // non-JSON diagnostics — ignore on stdout
      this._handleSdkMessage(msg);
    }
  }

  _handleSdkMessage(msg) {
    switch (msg.type) {
      case "system":
        // init / status — nothing to render
        return;

      case "stream_event": {
        const ev = msg.event || {};
        if (ev.type === "content_block_delta" && ev.delta && typeof ev.delta.text === "string") {
          this._textStreamed = true;
          this.emit(this.id, { type: "token", text: ev.delta.text });
        }
        return;
      }

      case "assistant": {
        const blocks = (msg.message && msg.message.content) || [];
        for (const b of blocks) {
          if (b.type === "text") {
            // Only emit if partial deltas didn't already stream this text.
            if (!this._textStreamed && b.text) this.emit(this.id, { type: "token", text: b.text });
          } else if (b.type === "tool_use") {
            if (b.id) this._toolNames.set(b.id, b.name);
            this.emit(this.id, { type: "tool:start", name: b.name, detail: toolDetail(b.input) });
          }
        }
        return;
      }

      case "user": {
        // tool_result blocks → mark the matching tool done
        const blocks = (msg.message && msg.message.content) || [];
        for (const b of blocks) {
          if (b.type === "tool_result") {
            const name = this._toolNames.get(b.tool_use_id) || "tool";
            this.emit(this.id, { type: b.is_error ? "tool:fail" : "tool:done", name });
          }
        }
        return;
      }

      case "control_request": {
        const req = msg.request || {};
        if (req.subtype === "can_use_tool") {
          const detail = toolDetail(req.input);
          const label = req.title
            || req.description
            || `Allow ${req.tool_name}${detail ? `: ${detail}` : ""}?`;
          this.emit(this.id, { type: "confirm", id: msg.request_id, label });
        }
        return;
      }

      case "control_cancel_request":
        this.emit(this.id, { type: "__clear_confirm" });
        return;

      case "result": {
        this.busy = false;
        this._textStreamed = false;
        this._toolNames.clear();
        this.emit(this.id, {
          type: "result",
          status: msg.is_error ? "error" : "ok",
          response: typeof msg.result === "string" ? msg.result : "",
          model: msg.model || null,
          elapsedMs: msg.duration_ms || 0,
          error: msg.is_error ? (msg.result || "error") : undefined,
        });
        return;
      }

      default:
        return; // keep_alive, etc.
    }
  }

  run({ task }) {
    if (!this.proc || !this.ready) return false;
    if (this.busy) {
      this.emit(this.id, { type: "error", msg: "Session is busy — wait for the current turn to finish or interrupt it." });
      return false;
    }
    this.busy = true;
    this._textStreamed = false;
    this._write({ type: "user", message: { role: "user", content: task } });
    return true;
  }

  respondConfirm(requestId, answer) {
    const behavior = answer === "no"
      ? { behavior: "deny", message: "User denied this action." }
      : { behavior: "allow" }; // "yes" / "always"
    this._write({
      type: "control_response",
      response: { subtype: "success", request_id: requestId, response: behavior },
    });
  }

  interrupt() {
    this._write({ type: "control_request", request_id: randomUUID(), request: { subtype: "interrupt" } });
  }

  _write(obj) {
    if (this.proc && this.proc.stdin.writable) {
      this.proc.stdin.write(JSON.stringify(obj) + "\n");
    }
  }

  dispose() {
    if (!this.proc) return;
    const proc = this.proc;
    try { proc.stdin.end(); } catch { /* ignore */ }
    setTimeout(() => { try { proc.kill(); } catch { /* ignore */ } }, 1500);
    this.proc = null;
  }
}

class SessionManager {
  constructor(emit) {
    this.emit = emit;
    this.sessions = new Map();
    this._seq = 0;
  }

  async create(cwd) {
    const id = `s${++this._seq}_${Date.now().toString(36)}`;
    const session = new CliSession(id, cwd, this.emit);
    this.sessions.set(id, session);
    const ok = await session.start();
    return { id, ready: ok, cwd };
  }

  get(id) { return this.sessions.get(id); }
  run(id, payload) { const s = this.get(id); if (s) s.run(payload); }
  respondConfirm(id, confirmId, answer) { const s = this.get(id); if (s) s.respondConfirm(confirmId, answer); }
  interrupt(id) { const s = this.get(id); if (s) s.interrupt(); }
  close(id) { const s = this.get(id); if (s) { s.dispose(); this.sessions.delete(id); } }
  disposeAll() { for (const s of this.sessions.values()) s.dispose(); this.sessions.clear(); }
}

module.exports = { SessionManager };
