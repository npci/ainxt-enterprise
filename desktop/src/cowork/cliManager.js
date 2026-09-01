// SPDX-License-Identifier: Apache-2.0
"use strict";
// ─── CLI PROTOCOL TRACER ──────────────────────────────────────────────────────
// Set AINXT_CLI_TRACE=1 (in ainxt-desktop.bat or env) to enable.
// All raw stdin/stdout lines + process lifecycle events are written to:
//   %USERPROFILE%\.ainxt\cli-trace.log   (Windows)
//   ~/.ainxt/cli-trace.log               (Mac/Linux)
// Tail it in a terminal:  tail -f ~/.ainxt/cli-trace.log
// ─────────────────────────────────────────────────────────────────────────────
const _traceEnabled = process.env.AINXT_CLI_TRACE === "1";
const _os = require("os");
const _traceFile = require("path").join(_os.homedir(), ".ainxt", "cli-trace.log");
function _trace(tag, data) {
  if (!_traceEnabled) return;
  try {
    const line = `[${new Date().toISOString()}] [${tag}] ${typeof data === "string" ? data : JSON.stringify(data)}\n`;
    require("fs").appendFileSync(_traceFile, line);
  } catch { /* best-effort */ }
}
// ─────────────────────────────────────────────────────────────────────────────
// ── Models cache version sync ─────────────────────────────────────────────────
// The CLI refuses a models cache whose recorded version does not match its own,
// and re-fetches from the gateway. Where that fetch cannot succeed (for example
// behind a TLS-terminating proxy the CLI does not trust) the user is left with no
// model list at all, so we stamp the cache to the expected version and let the
// CLI use it. Override the expected version with AINXT_CLI_MODELS_CACHE_VERSION,
// or disable the whole workaround with AINXT_CLI_SKIP_MODELS_CACHE_SYNC=1.
//
// This is a workaround, not a design: it makes the CLI trust a cache it would
// otherwise reject, so a genuinely stale cache will be used as-is. Prefer fixing
// gateway trust (see AINXT_DESKTOP_TLS_INSECURE) over relying on this.
const MODELS_CACHE_VERSION = process.env.AINXT_CLI_MODELS_CACHE_VERSION || "3.0.0-beta";

function _syncModelsCacheVersion() {
  if (process.env.AINXT_CLI_SKIP_MODELS_CACHE_SYNC === "1") return;
  try {
    const cachePath = require("path").join(_os.homedir(), ".ainxt", "models_cache.json");
    if (!require("fs").existsSync(cachePath)) return;
    const cache = JSON.parse(require("fs").readFileSync(cachePath, "utf-8"));
    if (cache.ainxt_version !== MODELS_CACHE_VERSION) {
      cache.ainxt_version = MODELS_CACHE_VERSION;
      cache.fetched_at = new Date().toISOString();
      require("fs").writeFileSync(cachePath, JSON.stringify(cache, null, 2));
      _trace("MODELS_CACHE", `Stamped models_cache.json to ${MODELS_CACHE_VERSION}`);
    }
  } catch { /* best-effort */ }
}
_syncModelsCacheVersion();
// ─────────────────────────────────────────────────────────────────────────────

// TLS bypass is now opt-in. Previously AINXT_TLS_INSECURE: "1" was hardcoded
// so. Now opt-in via AINXT_DESKTOP_TLS_INSECURE=1.
const TLS_ENV = process.env.AINXT_DESKTOP_TLS_INSECURE === "1"
  ? { AINXT_TLS_INSECURE: "1", AINXT_INSECURE_TLS: "1" }
  : {};

/**
 * CliSession — drives the `ainxt` agent headless over the stream-json protocol.
 *
 * Spawns: `ainxt --full --print --input-format stream-json --output-format
 * stream-json --verbose --include-partial-messages --tools default` in the chosen
 * folder. The `--full` route normalises argv and adds `--bare`, so the CLI
 * authenticates against the AiNxt gateway rather than any provider directly. One
 * long-lived process is one multi-turn session: the CLI holds the context and we
 * feed each user turn on stdin, so there is no history to manage here.
 *
 * Wire format (newline-delimited JSON):
 *   stdin  (us → CLI): {type:"user", message:{role:"user", content:"<text>"}}
 *                      {type:"control_response", response:{subtype:"success",
 *                        request_id, response:{behavior:"allow"|"deny"}}}
 *                      {type:"control_request", request_id, request:{subtype:"interrupt"}}
 *   stdout (CLI → us): {type:"system",subtype:"init"} |
 *                      {type:"stream_event",event} |
 *                      {type:"assistant",message:{content:[text|tool_use]}} |
 *                      {type:"user",message:{content:[tool_result]}} |
 *                      {type:"control_request",request_id,request:{subtype:"can_use_tool",...}} |
 *                      {type:"result",subtype,result,is_error,duration_ms}
 */
const { spawn } = require("child_process");
const { randomUUID } = require("crypto");
const { resolveCliBinary, missingCliMessage } = require("./binary");
const { resolveProtocol } = require("./protocol");

// ── DoS-by-Loop guards (Checkmarx CWE-400) ──────────────────────────────────
// Hard ceiling on the stdout accumulation buffer. Any data beyond this limit
// is discarded so the tainted external input never drives an unbounded loop.
const MAX_STDOUT_BUFFER_BYTES = 10 * 1024 * 1024; // 10 MB
// Hard ceiling on lines processed per chunk. The loop iterates over a
// pre-split, length-capped array — NOT over the raw tainted string — so the
// iteration count is always bounded by this constant, not by external input.
const MAX_LINES_PER_CHUNK = 10_000;


// Default model when neither the caller nor the session specifies one.
// Was a hardcoded cloud id, so a deployment running its own models spawned
// the CLI with a model it could not route to.
const DEFAULT_CLI_MODEL = process.env.AINXT_DEFAULT_MODEL || "";
function toolDetail(input) {
  if (!input || typeof input !== "object") return "";
  const v = input.command || input.file_path || input.path || input.pattern || input.url || "";
  return v ? String(v).slice(0, 80) : "";
}

// See coworkSession.js's _realToolName for the full explanation: the ACP CLI
// wraps every MCP tool call behind a generic `use_tool`/`search_tool` meta-tool,
// so without this the UI only ever shows "use_tool"/"search_tool" chips instead
// of the real tool (e.g. an MCP server's actual tool name).
function _realToolName(name, rawInput) {
  if (rawInput && typeof rawInput === "object") {
    if (rawInput.tool_name) return rawInput.tool_name;                 // use_tool
    if (name === "search_tool" && rawInput.query) return `search_tool: "${rawInput.query}"`;
  }
  return name;
}

const MAX_DIFF_LINES = 240;

function _basename(p) {
  if (!p) return "";
  const s = String(p).replace(/\\/g, "/");
  return s.slice(s.lastIndexOf("/") + 1);
}

// Build a renderable diff from a mutating tool's input. Edit/MultiEdit carry
// old_string/new_string; Write carries the full new content. Returns
// {path, added, removed, isNew, lines:[{kind:"+"|"-"|"@@"|" ", line}]} or null.
function buildDiff(name, input) {
  if (!input || typeof input !== "object") return null;
  const path = input.file_path || input.path || "";

  const pushPair = (lines, oldStr, newStr) => {
    const oldLines = oldStr != null ? String(oldStr).split("\n") : [];
    const newLines = newStr != null ? String(newStr).split("\n") : [];
    oldLines.forEach((l) => lines.push({ kind: "-", line: l }));
    newLines.forEach((l) => lines.push({ kind: "+", line: l }));
    return { added: newLines.length, removed: oldLines.length };
  };

  let lines = [], added = 0, removed = 0, isNew = false;

  if (name === "Edit") {
    const r = pushPair(lines, input.old_string, input.new_string);
    added += r.added; removed += r.removed;
  } else if (name === "MultiEdit" && Array.isArray(input.edits)) {
    input.edits.forEach((e, i) => {
      if (i > 0) lines.push({ kind: "@@", line: "" });
      const r = pushPair(lines, e.old_string, e.new_string);
      added += r.added; removed += r.removed;
    });
  } else if (name === "Write") {
    isNew = true; // we don't have prior content; show as added
    const newLines = String(input.content ?? "").split("\n");
    newLines.forEach((l) => lines.push({ kind: "+", line: l }));
    added = newLines.length;
  } else {
    return null;
  }

  let truncated = 0;
  if (lines.length > MAX_DIFF_LINES) {
    truncated = lines.length - MAX_DIFF_LINES;
    lines = lines.slice(0, MAX_DIFF_LINES);
  }
  return { path, name: _basename(path), added, removed, isNew, lines, truncated };
}

class CliSession {
  constructor(id, cwd, emit, resumeId) {
    this.id = id;
    this.cwd = cwd;
    this.emit = emit;
    this.resumeId = resumeId || null;   // resume an existing on-disk session
    this.sessionId = resumeId || null;  // real agent session_id (from init)
    this.proc = null;
    this.ready = false;
    this.busy = false;
    this._stdoutBuf = "";
    this._readyResolvers = [];
    this._toolNames = new Map();   // tool_use_id → name (for tool:done)
    this._pendingPerms = new Map(); // control request_id → {tool, input}
    this._pendingCtrl = new Map();  // our control request_id → {resolve, reject}
    this._lastTotalCost = 0;        // cumulative cost at the previous result (for per-turn delta)
    this._deltasSinceAssistant = false;
    this._streamBuffer = "";        // accumulates agent_message_chunk text for final result
    this._currentModel = null;      // tracks active model for per-turn switching
    // Which CLI wire protocol this session drives (see ./protocol.js).
    // "streamjson" is the default; "acp" is opt-in.
    this._protocol = resolveProtocol();
  }

  start() {
    const bin = resolveCliBinary();
    if (!bin) {
      this.emit(this.id, { type: "error", msg: missingCliMessage() });
      this.emit(this.id, { type: "session:exit", code: -1 });
      return Promise.resolve(false);
    }

    const cwd = this.cwd || process.cwd();
    this._cwd = cwd;
    const REPO_PROMPT =
      "You are AiNxt's local coding agent operating INSIDE the user's current " +
      "working directory, which is their code repository. The code is already on " +
      "disk here — NEVER ask the user to paste, share, or upload code. Always use " +
      "your tools (Bash, Read, Glob, Grep, Edit, Write) to explore and read the " +
      "project's files yourself. For any question about \"this code\", \"this " +
      "codebase\", improvements, bugs, or architecture, inspect the actual files " +
      "in the working directory first, then answer.";
    this._repoPrompt = REPO_PROMPT;

    // ── OLD CLI (v1.0.2-beta): single-shot `--json`, NO persistent process ──
    // This binary has no streaming/agent protocol — each turn spawns `--json`
    // and exits (see _runLegacy). Just mark ready.
    if (this._protocol === "streamjson") {
      this.ready = true;
      _trace("SPAWN", { protocol: "streamjson", mode: "single-shot --json", cwd });
      this._readyResolvers.forEach((r) => r(true));
      this._readyResolvers = [];
      return Promise.resolve(true);
    }

    // ── ACP protocol — persistent `agent stdio` ───────────────────────────
    const _acpModel = DEFAULT_CLI_MODEL;
    const args = [
      ...bin.args,
      "--cwd",   cwd,
      ...(_acpModel ? ["--model", _acpModel] : []),
      "agent", "stdio",
    ];
    if (this.resumeId) args.push("--resume", this.resumeId);

    // ── TRACE: log the exact command + all args before spawning ──────────────
    _trace("SPAWN", { binary: bin.command, mode: bin.mode, protocol: this._protocol, cwd, args });
    // ─────────────────────────────────────────────────────────────────────────

    this.proc = spawn(bin.command, args, {
      cwd,
      env: {
        ...process.env,
        FORCE_COLOR: "0",
        AINXT_IS_COWORK: "1",
        // Accept a self-signed gateway TLS cert. The names differ per CLI:
        ...TLS_ENV,
      },
    });

    this.proc.stdout.setEncoding("utf-8");
    this.proc.stdout.on("data", (chunk) => this._onStdout(chunk));
    this.proc.stderr.on("data", (d) => {
      const text = d.toString().trim();
      if (text) {
        _trace("STDERR", text); // ← TRACE: every stderr line from the CLI
        this.emit(this.id, { type: "notice", msg: text, level: "info" });
      }
    });
    this.proc.on("error", (err) => {
      _trace("PROC_ERROR", { message: err.message, code: err.code }); // ← TRACE
      this.emit(this.id, { type: "error", msg: "Something went wrong starting AiNxt. Please try again or reinstall the application." });
    });
    this.proc.on("spawn", () => {
      _trace("PROC_SPAWN", `process started successfully (protocol=${this._protocol})`); // ← TRACE
      // Only the ACP path reaches here (streamjson early-returns without a proc).
      // Run ACP handshake (initialize → authenticate → session/new) and mark
      // ready only after session/new so we have a sessionId before first prompt.
      this._initialize().then(() => {
        this.ready = true;
        this._readyResolvers.forEach((r) => r(true));
        this._readyResolvers = [];
      }).catch((err) => {
        _trace("INIT_FAILED", err.message);
        this.ready = false;
        this._readyResolvers.forEach((r) => r(false));
        this._readyResolvers = [];
        this.emit(this.id, { type: "error", msg: `Session init failed: ${err.message}` });
      });
    });
    this.proc.on("close", (code, signal) => {
      _trace("PROC_CLOSE", { code, signal }); // ← TRACE: exit code + signal
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
    // Split into lines immediately. Last element is an incomplete line — carry forward.
    const allLines = (this._stdoutBuf + chunk).split("\n");
    this._stdoutBuf = allLines.pop() ?? "";

    // Cap processing to a constant number of lines per chunk.
    allLines.slice(0, MAX_LINES_PER_CHUNK).forEach((line) => {
      const trimmed = line.trim();
      if (!trimmed) return;

      _trace("STDOUT_RAW", trimmed);

      let msg;
      try {
        msg = JSON.parse(trimmed);
      } catch {
        _trace("STDOUT_NOT_JSON", trimmed);
        return;
      }

      _trace("STDOUT_MSG_TYPE", msg.type || "(no type)");
      this._handleSdkMessage(msg);
    });
  }

  // Persistent-process stdout handler — ACP only. The OLD CLI is single-shot
  // `--json` and parses its one-object output inline in _runLegacy().
  _handleSdkMessage(msg) {
    // JSON-RPC response (id + result/error, no method) — resolves a pending request
    if (msg.id !== undefined && msg.method === undefined) {
      const pend = this._pendingCtrl.get(msg.id);
      if (pend) {
        this._pendingCtrl.delete(msg.id);
        if (msg.error) pend.reject(new Error(msg.error.message || "rpc error"));
        else pend.resolve(msg.result || {});
      }
      // If this is the result of the current session/prompt turn, mark done
      if (msg.id === this._currentTurnId) {
        this.busy = false;
        this._deltasSinceAssistant = false;
        this._toolNames.clear();
        this._pendingPerms.clear();
        const r = msg.result || {};
        const meta = r._meta || {};
        // v0.2.101 streams text via agent_message_chunk; use accumulated buffer as response
        const responseText = this._streamBuffer || (typeof r.text === "string" ? r.text : "");
        this._streamBuffer = "";
        const totalTokens = meta.totalTokens || 0;
        this.emit(this.id, {
          type: "result",
          status: msg.error ? "error" : "ok",
          response: responseText,
          model: meta.modelId || r.model || null,
          elapsedMs: r.duration_ms || 0,
          costUsd: 0,
          costTotalUsd: this._lastTotalCost,
          usage: { input: meta.inputTokens || r.input_tokens || 0, output: meta.outputTokens || r.output_tokens || 0 },
          numTurns: r.num_turns || 0,
          error: msg.error ? (msg.error.message || "error") : undefined,
        });
        // Emit context usage so the UI context bar stays up to date.
        this.getContextUsage().then((d) => {
          if (d && typeof d.percentage !== "undefined") {
            this.emit(this.id, { type: "context", pct: d.percentage, tokens: d.totalTokens, max: d.rawMaxTokens });
          }
        }).catch(() => {});
      }
      return;
    }

    // JSON-RPC notification/request from CLI (has method)
    const method = msg.method || "";
    const params = msg.params || {};

    switch (method) {

      // ── Streaming text delta ──────────────────────────────────────────────
      case "session/update": {
        const update = params.update || {};
        // agent_message_chunk (v0.2.101+) OR assistant_message_chunk (older) — streaming token
        if ((update.sessionUpdate === "agent_message_chunk" || update.sessionUpdate === "assistant_message_chunk") && update.content) {
          const text = typeof update.content === "string" ? update.content
            : (update.content.text || "");
          if (text) {
            this._deltasSinceAssistant = true;
            this._streamBuffer += text;   // accumulate for final result
            this.emit(this.id, { type: "token", text });
          }
        }
        // tool_use_start (old) OR tool_call (v0.2.101+) — tool is starting
        else if (update.sessionUpdate === "tool_use_start" && update.tool) {
          const name = update.tool.name || "tool";
          if (update.tool.id) this._toolNames.set(update.tool.id, name);
          this.emit(this.id, {
            type: "tool:start",
            name,
            detail: toolDetail(update.tool.input),
            diff: buildDiff(name, update.tool.input),
          });
        }
        else if (update.sessionUpdate === "tool_call") {
          // v0.2.101: tool_call fires when the AI decides to use a tool
          const wrapperName = update.title || "tool";
          const rawInput = update.rawInput || {};
          // Unwrap use_tool/search_tool to show the REAL tool being called.
          const name = _realToolName(wrapperName, rawInput);
          const innerInput = (rawInput && rawInput.tool_input) || rawInput;
          const toolId = update.toolCallId;
          if (toolId) this._toolNames.set(toolId, name);
          this.emit(this.id, {
            type: "tool:start",
            name,
            detail: toolDetail(innerInput),
            diff: buildDiff(name, innerInput),
          });
        }
        // tool_use_result (old) OR tool_call_update with status completed (v0.2.101+)
        else if (update.sessionUpdate === "tool_use_result" && update.tool_use_id) {
          const name = this._toolNames.get(update.tool_use_id) || "tool";
          this.emit(this.id, { type: update.is_error ? "tool:fail" : "tool:done", name });
        }
        else if (update.sessionUpdate === "tool_call_update") {
          // v0.2.101: tool_call_update fires as tool progresses / completes
          const toolId = update.toolCallId;
          const name = (toolId && this._toolNames.get(toolId)) || update.title || "tool";
          const status = (update.status || "").toLowerCase();
          if (status === "completed") {
            this.emit(this.id, { type: "tool:done", name });
          } else if (status === "failed" || status === "error") {
            // Surface WHY it failed (see coworkSession.js for the full rationale) —
            // LOGGED ONLY, deliberately not shown in the UI (detail omitted from
            // the emitted event); the chip stays a plain red X for the user.
            const rawOut = update.rawOutput || {};
            const contentText = Array.isArray(update.content) && update.content[0]
              ? (update.content[0].content && update.content[0].content.text) || ""
              : "";
            const reason = rawOut.message || contentText || "(no error detail provided by CLI)";
            _trace("TOOL_CALL_FAILED", { name, toolId, reason });
            this.emit(this.id, { type: "tool:fail", name });
          }
          // status null / pending / in-progress — no event needed, already shown via tool:start
        }
        // turn_complete / assistant_turn_complete / turn_completed (v0.2.101+)
        else if (update.sessionUpdate === "turn_complete" || update.sessionUpdate === "assistant_turn_complete" || update.sessionUpdate === "turn_completed") {
          this.busy = false;
          this._deltasSinceAssistant = false;
          this._toolNames.clear();
          this._pendingPerms.clear();
          const totalCost = typeof update.total_cost_usd === "number" ? update.total_cost_usd : this._lastTotalCost;
          const turnCost = Math.max(0, totalCost - this._lastTotalCost);
          this._lastTotalCost = totalCost;
          this.emit(this.id, {
            type: "result",
            status: update.error ? "error" : "ok",
            response: typeof update.text === "string" ? update.text : "",
            model: update.model || null,
            elapsedMs: update.duration_ms || 0,
            costUsd: turnCost,
            costTotalUsd: totalCost,
            usage: { input: update.input_tokens || 0, output: update.output_tokens || 0 },
            numTurns: update.num_turns || 0,
            error: update.error || undefined,
          });
        }
        // available_commands_update — slash commands + tools list
        else if (update.sessionUpdate === "available_commands_update") {
          const cmds = Array.isArray(update.availableCommands) ? update.availableCommands : [];
          const tools = Array.isArray((update._meta || {}).tools) ? update._meta.tools : [];
          this.emit(this.id, {
            type: "session:init",
            model: null,
            permissionMode: "default",
            slashCommands: cmds.map((c) => ({
              name: c.name,
              description: c.description || "",
              argumentHint: (c.input && c.input.hint) || "",
            })),
            tools,
            skills: [],
          });
        }
        return;
      }

      // ── Permission request ────────────────────────────────────────────────
      case "agent/confirmTool":
      case "agent/confirm": {
        const reqId = msg.id;
        const toolName = params.name || params.tool || "tool";
        const input = params.input || {};
        if (reqId !== undefined && reqId !== null) this._pendingPerms.set(reqId, { tool: toolName, input });
        const detail = toolDetail(input);
        this.emit(this.id, {
          type: "confirm",
          id: reqId,
          tool: toolName,
          detail,
          label: `Allow ${toolName}${detail ? `: ${detail}` : ""}?`,
        });
        return;
      }

      // ── session/prompt_complete — turn finished (v0.2.101+) ──────────────
      // Fired after the JSON-RPC result for session/prompt; use as a safety
      // net to ensure busy is cleared even if the RPC result arrives out of order.
      case "_ainxt.dev/session/prompt_complete": {
        if (this.busy) {
          this.busy = false;
          this._deltasSinceAssistant = false;
          this._toolNames.clear();
          this._pendingPerms.clear();
          const responseText = this._streamBuffer;
          this._streamBuffer = "";
          this.emit(this.id, {
            type: "result",
            status: "ok",
            response: responseText,
            model: null,
            elapsedMs: 0,
            costUsd: 0,
            costTotalUsd: this._lastTotalCost,
            usage: { input: 0, output: 0 },
            numTurns: 0,
          });
        }
        return;
      }

      // ── session_notification — may carry turn_completed ───────────────────
      case "_ainxt.dev/session_notification": {
        const upd = params.update || {};
        if (upd.sessionUpdate === "turn_completed" && this.busy) {
          // turn is done; the JSON-RPC result will also arrive and emit "result",
          // so just reset busy here as a safety net if RPC result is delayed.
          // Don't emit "result" here — let the RPC response do it with full data.
          const usage = upd.usage || {};
          this._lastTotalCost = 0; // cost not available here
          _trace("TURN_COMPLETED_NOTIFICATION", { stopReason: upd.stop_reason, tokens: usage.totalTokens });
        }
        return;
      }

      // ── session/request_permission — tool execution gate (v0.2.101+) ──────
      // New CLI sends this instead of agent/confirm. Response: { optionId }.
      // Show the same confirm dialog so the user sees Allow / Don't Allow.
      // ── session/request_permission — tool execution gate (v0.2.101+) ──────
      // Show Allow / Don't Allow dialog. id is integer 0 — store and use
      // _writeLine in respondConfirm to preserve the integer type.
      case "session/request_permission": {
        const reqId = msg.id;
        const toolCall = params.toolCall || {};
        const toolName = toolCall.title || toolCall.tool || "tool";
        const input = (toolCall.rawInput || {}).tool_input || toolCall.rawInput || {};
        const options = params.options || [];
        if (reqId !== undefined && reqId !== null) {
          this._pendingPerms.set(reqId, { tool: toolName, input, options, isPermissionRequest: true });
        }
        const detail = toolDetail(input);
        this.emit(this.id, { type: "confirm", id: reqId, tool: toolName, detail, label: `Allow ${toolName}${detail ? `: ${detail}` : ""}?` });
        return;
      }

      // ── ask_user_question — CLI is waiting for user input ────────────────
      // The CLI sends this as a JSON-RPC *request* (has an id) and blocks until
      // we reply. The desktop has no interactive question UI, so we auto-answer
      // with the first (recommended) option from each question and notify the
      // user via a token so they can see what was chosen.
      case "_ainxt.dev/ask_user_question": {
        const reqId = msg.id;           // must echo this back in our response
        const questions = params.questions || [];
        _trace("ASK_USER_QUESTION", { reqId, count: questions.length });

        // Build answers: for each question pick the first option's label.
        // multiSelect questions get an array; single-select gets a string.
        const answers = questions.map((q) => {
          const first = (q.options && q.options[0]) ? q.options[0].label : "";
          return q.multiSelect ? [first] : first;
        });

        // Tell the user (via a token in the chat) what was auto-selected.
        const summary = questions.map((q, i) => {
          const chosen = Array.isArray(answers[i]) ? answers[i].join(", ") : answers[i];
          return `**${q.question}**\n→ Auto-selected: *${chosen}*`;
        }).join("\n\n");
        this.emit(this.id, { type: "token", text: `\n\n📋 *The AI asked for your input — auto-answering with recommended options:*\n\n${summary}\n\n` });

        // Send the JSON-RPC response back to unblock the CLI.
        this._writeRpcResponse(reqId, { answers });
        return;
      }

      // ── exit_plan_mode — CLI is waiting for plan approval ────────────────
      // The CLI sends this as a JSON-RPC request (has an id) with the plan
      // content and blocks until we respond with { approved: true/false }.
      // Auto-approve so the agent proceeds immediately; show the plan to the user.
      case "_ainxt.dev/exit_plan_mode": {
        const reqId = msg.id;
        const planContent = params.planContent || "";
        if (planContent) {
          this.emit(this.id, { type: "token", text: `\n\n📋 **Plan ready — proceeding with implementation:**\n\n${planContent}\n\n` });
        }
        this._writeRpcResponse(reqId, { approved: true });
        return;
      }

      // ── Benign notifications — ignore ─────────────────────────────────────
      case "_ainxt.dev/mcp/servers_updated":
      case "_ainxt.dev/mcp_initialized":
      case "_ainxt.dev/sessions/changed":
      case "_ainxt.dev/queue/changed":
        return;

      default:
        _trace("UNHANDLED_MSG_TYPE", `method=${method} params=${JSON.stringify(params).slice(0, 200)}`);
        return;
    }
  }

  run({ task, model }) {
    if (!this.ready) return false;
    if (this.busy) {
      this.emit(this.id, { type: "error", msg: "Session is busy — wait for the current turn to finish or interrupt it." });
      return false;
    }
    if (model) this._currentModel = model;

    // ── OLD CLI: single-shot `--json` turn (no persistent process) ─────────
    if (this._protocol === "streamjson") {
      return this._runLegacy(task, model);
    }

    // ── NEW CLI: streaming ACP turn ────────────────────────────────────────
    if (!this.proc) return false;
    if (model && model !== this._currentModel) this.setModel(model).catch(() => {});
    this.busy = true;
    this._deltasSinceAssistant = false;
    this._streamBuffer = "";          // clear accumulator for new turn
    this._currentTurnId = randomUUID();
    this._writeRpc(this._currentTurnId, "session/prompt", {
      sessionId: this.sessionId,
      prompt: [{ type: "text", text: task }],
    });
    return true;
  }

  // OLD CLI single-shot turn: spawn `--json`, capture one object, emit token+result.
  _runLegacy(task, model) {
    const bin = resolveCliBinary();
    if (!bin) { this.emit(this.id, { type: "error", msg: missingCliMessage() }); return false; }
    this.busy = true;
    const cwd = this._cwd || process.cwd();
    const _jsonModel = model || this._currentModel || DEFAULT_CLI_MODEL;
    const args = [
      ...bin.args,
      "--json",
      ...(_jsonModel ? ["--model", _jsonModel] : []),
      "--add-dir", cwd,
      "--append-system-prompt", this._repoPrompt || "",
      task,
    ];
    _trace("SPAWN", { protocol: "streamjson", mode: "--json turn", cwd });

    let out = "", err = "";
    const proc = spawn(bin.command, args, {
      cwd,
      env: { ...process.env, FORCE_COLOR: "0", AINXT_IS_COWORK: "1", ...TLS_ENV },
    });
    this.proc = proc;
    proc.stdout.on("data", (d) => { out += d.toString(); });
    proc.stderr.on("data", (d) => { const t = d.toString(); err += t; if (t.trim()) _trace("STDERR", t.trim()); });
    proc.on("error", (e) => {
      _trace("PROC_ERROR", { message: e.message });
      this.busy = false;
      this.emit(this.id, { type: "error", msg: "Something went wrong running AiNxt. Please try again." });
    });
    proc.on("close", (code) => {
      this.busy = false;
      this.proc = null;
      let obj = null;
      const jsonStart = out.indexOf("{");
      if (jsonStart >= 0) { try { obj = JSON.parse(out.slice(jsonStart)); } catch { /* fall through */ } }
      if (obj && typeof obj.response === "string") {
        this.emit(this.id, { type: "token", text: obj.response });
        this.emit(this.id, {
          type: "result",
          status: obj.status === "ok" ? "ok" : "error",
          response: obj.response,
          model: obj.model || null,
          elapsedMs: obj.elapsed_ms || 0,
          costUsd: 0, costTotalUsd: this._lastTotalCost,
          usage: { input: 0, output: 0 }, numTurns: 1,
          error: obj.status === "ok" ? undefined : (obj.response || "error"),
        });
      } else {
        const detail = (err.trim() || out.trim() || `exited with code ${code}`).slice(0, 400);
        _trace("LEGACY_TURN_FAILED", { code, detail });
        this.emit(this.id, { type: "result", status: "error", response: "", error: detail, model: null, elapsedMs: 0, costUsd: 0, costTotalUsd: this._lastTotalCost, usage: { input: 0, output: 0 }, numTurns: 0 });
      }
    });
    return true;
  }

  respondConfirm(requestId, answer) {
    const perm = this._pendingPerms.get(requestId);
    this._pendingPerms.delete(requestId);
    const allowed = answer !== "no";

    // OLD CLI is single-shot `--json` and never surfaces per-tool confirms.
    if (this._protocol === "streamjson") return;

    // ── NEW CLI (ACP) ──────────────────────────────────────────────────────
    if (perm && perm.isPermissionRequest) {
      // session/request_permission: SelectedPermissionOutcome {optionId, kind}.
      // _writeLine preserves integer id type (CLI sent id=0 as integer).
      const options = perm.options || [];
      const chosen = allowed
        ? options.find((o) => o.kind === "allow_once" || o.optionId === "allow-once")
        : options.find((o) => o.kind === "reject_once" || o.optionId === "reject-once");
      const optionId = chosen ? chosen.optionId : (allowed ? "allow-once" : "reject-once");
      const kind = chosen ? chosen.kind : (allowed ? "allow_once" : "reject_once");
      // RequestPermissionOutcome uses outcome field with values: approve/reject
      const result = { outcome: allowed ? "approve" : "reject" };
      this._writeLine({ jsonrpc: "2.0", id: requestId, result });
    } else {
      this._writeRpcResponse(requestId, { allowed, reason: allowed ? null : "User denied." });
    }
  }

  interrupt() {
    if (this._protocol === "streamjson") {
      // OLD CLI: kill the in-flight single-shot `--json` process, if any.
      if (this.proc) { try { this.proc.kill(); } catch { /* ignore */ } this.proc = null; }
      this.busy = false;
      return;
    }
    this._writeRpcNotification("notifications/cancelled", {
      requestId: this._currentTurnId || "unknown",
      reason: "user interrupted",
    });
  }

  _sendRpc(method, params = {}) {
    return new Promise((resolve, reject) => {
      if (!this.proc || !this.ready) return reject(new Error("session not ready"));
      const id = randomUUID();
      this._pendingCtrl.set(id, { resolve, reject });
      this._writeRpc(id, method, params);
      setTimeout(() => {
        if (this._pendingCtrl.has(id)) {
          this._pendingCtrl.delete(id);
          reject(new Error(`RPC ${method} timed out`));
        }
      }, 15000);
    });
  }

  // Only the ACP path has a persistent process to handshake with; the OLD
  // single-shot CLI has no handshake (each `--json` turn is self-contained).
  async _initialize() {
    if (this._protocol === "streamjson") return;
    return this._initializeAcp();
  }

  // ACP handshake: initialize → authenticate → session/new
  async _initializeAcp() {
    try {
      // 1. initialize
      const initId = randomUUID();
      this._writeRpc(initId, "initialize", {
        protocolVersion: 1,
        clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false, auth: { terminal: false } },
        _meta: { clientType: "ainxt-desktop", clientVersion: "1.0.0",
          startupHints: { nonInteractive: true, skipGitStatus: true, skipProjectLayout: true } },
      });
      const initResult = await new Promise((resolve, reject) => {
        this._pendingCtrl.set(initId, { resolve, reject });
        setTimeout(() => { this._pendingCtrl.delete(initId); reject(new Error("initialize timed out")); }, 10000);
      });

      // 2. authenticate
      const authId = randomUUID();
      this._writeRpc(authId, "authenticate", { methodId: "ainxt.api_key", _meta: { headless: true } });
      await new Promise((resolve, reject) => {
        this._pendingCtrl.set(authId, { resolve, reject });
        setTimeout(() => { this._pendingCtrl.delete(authId); reject(new Error("authenticate timed out")); }, 10000);
      });

      // 3. session/new
      const sessionNewId = randomUUID();
      this._writeRpc(sessionNewId, "session/new", { cwd: this.cwd || process.cwd(), mcpServers: [] });
      const sessionResult = await new Promise((resolve, reject) => {
        this._pendingCtrl.set(sessionNewId, { resolve, reject });
        setTimeout(() => { this._pendingCtrl.delete(sessionNewId); reject(new Error("session/new timed out")); }, 15000);
      });

      // Store sessionId + emit session:init
      const meta = initResult._meta || {};
      const sessionId = (sessionResult && sessionResult.sessionId) || meta.agentInstanceId || null;
      if (sessionId) { this.sessionId = sessionId; this.emit(this.id, { type: "session:id", sessionId }); }
      const cmds = Array.isArray(meta.availableCommands) ? meta.availableCommands : [];
      this.emit(this.id, {
        type: "session:init",
        model: (initResult.modelState && initResult.modelState.currentModelId) || null,
        permissionMode: "default",
        slashCommands: cmds.map((c) => ({ name: c.name, description: c.description || "", argumentHint: (c.input && c.input.hint) || "" })),
        tools: [], skills: [],
      });
    } catch (e) { _trace("INIT_ERROR", e.message); }
  }

  async setModel(model) {
    // Both paths set the model per-spawn (--model), so just remember it.
    if (model) this._currentModel = model;
    return true;
  }

  async setPermissionMode(mode) {
    // Neither the single-shot OLD CLI nor ACP v0.2.101 support runtime mode change.
    return true;
  }

  async getContextUsage() {
    // Neither the single-shot OLD CLI nor ACP v0.2.101 expose context usage.
    return null;
  }

  _writeRpc(id, method, params) { this._writeLine({ jsonrpc: "2.0", id, method, params }); }
  _writeRpcNotification(method, params) { this._writeLine({ jsonrpc: "2.0", method, params }); }
  _writeRpcResponse(id, result) { this._writeLine({ jsonrpc: "2.0", id, result }); }

  _writeLine(obj) {
    if (this.proc && this.proc.stdin.writable) {
      const line = JSON.stringify(obj);
      _trace("STDIN_SEND", line);
      this.proc.stdin.write(line + "\n");
    } else {
      _trace("STDIN_BLOCKED", { reason: !this.proc ? "no proc" : "stdin not writable", obj });
    }
  }

  dispose() {
    // OLD single-shot sessions have no persistent process, so emit session:exit
    // once here (killing any in-flight `--json` turn first) so the UI cleans up.
    if (this._protocol === "streamjson") {
      if (this.proc) { try { this.proc.kill(); } catch { /* ignore */ } this.proc = null; }
      if (!this._exitEmitted) {
        this._exitEmitted = true;
        this.ready = false;
        this.emit(this.id, { type: "session:exit", code: 0 });
      }
      return;
    }
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

  async create(cwd, resumeId) {
    const id = `s${++this._seq}_${Date.now().toString(36)}`;
    const session = new CliSession(id, cwd, this.emit, resumeId);
    this.sessions.set(id, session);
    const ok = await session.start();
    return { id, ready: ok, cwd, resumeId: resumeId || null };
  }

  get(id) { return this.sessions.get(id); }
  run(id, payload) { const s = this.get(id); if (s) s.run(payload); }
  respondConfirm(id, confirmId, answer) { const s = this.get(id); if (s) s.respondConfirm(confirmId, answer); }
  interrupt(id) { const s = this.get(id); if (s) s.interrupt(); }
  setModel(id, model) { const s = this.get(id); return s ? s.setModel(model) : Promise.resolve(false); }
  setPermissionMode(id, mode) { const s = this.get(id); return s ? s.setPermissionMode(mode) : Promise.resolve(false); }
  getContextUsage(id) { const s = this.get(id); return s ? s.getContextUsage() : Promise.resolve(null); }
  close(id) { const s = this.get(id); if (s) { s.dispose(); this.sessions.delete(id); } }
  disposeAll() { for (const s of this.sessions.values()) s.dispose(); this.sessions.clear(); }
}

module.exports = { SessionManager };
