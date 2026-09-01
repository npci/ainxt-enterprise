"use strict";
/**
 * CoworkOfficeSession — drives the FULL ainxt agent (app/main.tsx) headless over
 * the Claude Agent-SDK stream-json protocol, configured as an OFFICE assistant
 * (the "Buddy" tab) rather than the in-repo coding agent (the "Code" tab).
 *
 * This is the local-agent half of the P0 connector bridge: the agent runs on the
 * user's machine but reaches NPCI connectors, the Knowledge Base, and documents
 * through the gateway's Buddy MCP server, exposed as an `sse` MCP server pointed
 * at <gatewayBase>/ainxt/v1/api/cowork/mcp/sse and authenticated with the user's
 * AiNxt JWT. The agent therefore NEVER asks the user to paste data — it calls its
 * connector / KB / doc tools instead.
 *
 * It is modelled CLOSELY on ./cliManager.js (the Code tab's headless driver) and
 * reuses that file's EXACT stream-json protocol parsing, permission can_use_tool
 * confirms, cost/context events, and diff/tool events, and emits the SAME event
 * vocabulary, so a renderer mirroring Code.jsx works unchanged.
 *
 * Differences from cliManager.js (CliSession):
 *   1. Office system prompt (--append-system-prompt): an office assistant that
 *      uses connectors / KB / docs and NEVER asks the user to paste — it uses its
 *      tools. (cliManager injects a "you are inside the repo, read files" prompt.)
 *   2. Gateway connector MCP injected via --mcp-config as an `sse` server with an
 *      Authorization: Bearer <jwt> header, so the local agent can call connectors
 *      and the KB through the gateway. (cliManager has no MCP wiring.)
 *   3. Sub-agents are ALLOWED (Task tool is not stripped). (cliManager is a
 *      single-agent coding loop.)
 *
 * NPCI guardrails (enforced server-side; honoured here by design):
 *   - Reads through the gateway MCP are compliance-REDACTED, never blocked — the
 *     user always gets an answer.
 *   - Outbound connector / doc WRITES (send email, post message, create doc, …)
 *     are HARD-BLOCKED on sensitive content and never auto-execute: they require
 *     the existing confirm + compliance-gated path (POST /connectors/action,
 *     workers/doc_worker.py). We therefore route every tool through the
 *     can_use_tool confirm dialog (no write tool is pre-allowed) and NEVER log
 *     secrets or the JWT.
 *
 * Wire format & event vocabulary are IDENTICAL to cliManager.js — see its header.
 */
const { spawn } = require("child_process");
const { randomUUID } = require("crypto");
const os = require("os");
const path = require("path");
const fs = require("fs");
const { resolveCliBinary } = require("./binary");

// Connector tools that SEND to other people or IRREVERSIBLY change state. These
// must ALWAYS confirm (never auto-allowed by accept/bypass permission modes) — the
// MCP tool name is `mcp__ainxt_cowork__<tool>` or the bare connector tool name.
// Matched by suffix so both forms work. (G9)
const _DESTRUCTIVE_TOOL_SUFFIXES = [
  // outbound sends
  "outlook_send_mail", "outlook_send_draft", "outlook_reply_email",
  "outlook_reply_all_email", "outlook_forward_email",
  "teams_send_message", "teams_send_chat_message", "teams_reply_channel_message",
  "teams_start_chat", "teams_create_channel", "teams_create_online_meeting",
  "calendar_create_event", "calendar_forward_event",
  // destructive / state-changing
  "outlook_delete_email", "outlook_move_email", "outlook_mark_email",
  "outlook_create_folder", "outlook_create_draft",
  "calendar_update_event", "calendar_cancel_event", "calendar_delete_event",
  "calendar_accept_event", "calendar_decline_event", "calendar_tentative_event",
  "onedrive_upload",
];
function _isDestructiveConnectorTool(toolName) {
  const n = String(toolName || "");
  return _DESTRUCTIVE_TOOL_SUFFIXES.some((s) => n === s || n.endsWith("__" + s) || n.endsWith(s));
}

function toolDetail(input) {
  if (!input || typeof input !== "object") return "";
  const v = input.command || input.file_path || input.path || input.pattern || input.url || "";
  return v ? String(v).slice(0, 80) : "";
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

// Office system prompt — the Buddy counterpart of cliManager's REPO_PROMPT.
// Tells the agent it is an office assistant wired to NPCI connectors / KB / docs
// via its MCP tools, so it must NEVER ask the user to paste data, and that any
// outbound write (email / message / doc) is confirmed + compliance-gated.
const OFFICE_PROMPT =
  "You are AiNxt Buddy — an AI OFFICE ASSISTANT for an NPCI employee (a NON-engineer). " +
  "You do knowledge work: read and summarize emails/messages/documents, draft replies and " +
  "updates, prepare reports, and pull information from the user's connected apps — using ONLY " +
  "the office tools provided to you via your MCP servers (connectors, documents, calculations, " +
  "and — only when explicitly listed in your tools — browser & computer control).\n" +
  "YOU ARE NOT A DEVELOPER. You have NO terminal, NO shell, NO Bash, and you must NEVER attempt to " +
  "run OS commands, curl/HTTP an API, edit code, or inspect the user's system. If a task seems to " +
  "need that, instead use a connector or a document tool — or tell the user it's not available.\n" +
  "FILE ACCESS — be accurate about this: you can ONLY work with files INSIDE the user's attached folder. " +
  "To see what files are in it, call the `list_files` tool (it lists exactly the attached folder). The " +
  "files are also listed below under '## Files in your attached folder'. To open one, call Read on its " +
  "full path. You CANNOT read any path outside that folder (/etc, the home directory, other folders) — " +
  "such reads are blocked. NEVER claim you can read 'any file on the machine if they give a path' (false), " +
  "and never say the folder is empty if files are listed. If no folder is attached, you have NO local file " +
  "access — call list_files only after one is attached; otherwise use connectors.\n" +
  "FOR CALCULATIONS / DATA WORK you DO have one tool: `run_code`. It runs a short script in a SECURE, " +
  "network-isolated, throwaway SANDBOX (not the user's machine, no internet, destroyed after each run) " +
  "and returns only what the script prints. Use it for accurate math, totals, parsing/reshaping a " +
  "CSV/JSON, or date arithmetic — put the data into the script itself; the sandbox cannot open files " +
  "or fetch URLs. This is for computing answers, never for operating the computer. Write for a " +
  "non-technical audience: present results, not code.\n" +
  "DOCUMENTS (Word .docx, PowerPoint .pptx, Excel .xlsx, PDF) — produce professional, EDITABLE files using " +
  "the document SKILLS: (1) call `get_document_skill` with the format to read the exact rules and code " +
  "patterns; (2) write the build code it tells you to (docx-js JS for docx, pptxgenjs JS for pptx, Python " +
  "openpyxl for xlsx, docx-js JS for pdf) following the skill's styling guidance; (3) call `build_document` " +
  "with that code. CRITICAL: NEVER call build_document with an empty or partial `code` — write the COMPLETE " +
  "script FIRST (it must write the output file, e.g. fs.writeFileSync('output.docx', …)), THEN call " +
  "build_document once with the full script. " +
  "It runs in the secure sandbox and returns a [DOCJOB:...] marker — include it VERBATIM so " +
  "the user gets a rendered preview + download. If the build reports an error, fix the code and call again. " +
  "To revise later, call build_document again with updated code. Use `generate_document` only for plain " +
  "Markdown. Always show the [DOCJOB:...] marker; never paste raw code to the user.\n" +
  "RESEARCH / ANALYSIS / 'brief me on' / 'compare' / 'write a report on' tasks — use the `deep_research` " +
  "tool. It runs AiNxt's multi-model engine (Claude + GPT cross-examined), which is more rigorous than " +
  "answering directly. FIRST gather real material with your connector/file tools (relevant emails, Teams " +
  "messages, SharePoint/Drive docs, attached files), then pass each item in `sources` so the report carries " +
  "real [n] citations; set `depth` to 'deep' for thorough asks. Relay the returned report to the user, and " +
  "offer to turn it into a Word/PDF/PPT file via the document skills.\n" +
  "NEVER ask the user to paste, forward, upload, or copy in emails, messages, tickets, or documents " +
  "— call the appropriate connector/document/browser tool to fetch them yourself, then act. Reading " +
  "is safe (the platform redacts sensitive data on the way back). Any OUTBOUND action — sending an " +
  "email/message, posting to a connector, creating a document — goes through your action tools, which " +
  "are compliance-gated and require explicit user confirmation: propose it and let the confirm flow run. " +
  "Sub-agents (the Task tool) are available, but use them ONLY when the user EXPLICITLY asks to delegate " +
  "or run things in parallel (e.g. 'use sub-agents', 'in parallel', 'delegate each section'). By default, " +
  "do the work YOURSELF inline — it is faster and the user sees your progress stream. When the user does " +
  "ask to delegate, spawn a focused sub-agent per independent piece, then synthesise their results into " +
  "one clear answer — never expose raw sub-agent chatter.\n" +
  "MEMORY — you have a `remember` tool that saves a durable fact about THIS user to your long-term memory. " +
  "Whenever the user tells you something lasting about themselves — a preference, a like or dislike, how they " +
  "want things done, their role/team, recurring people or projects — proactively CALL `remember` with a short " +
  "note (e.g. \"Dislikes chocolate\"), WITHOUT being asked, then briefly confirm you'll remember it. The facts " +
  "already known about this user appear in your operating context — treat them as things you KNOW. NEVER reply " +
  "\"I don't know\" or \"I have no memory\" about something the user has told you or that is recorded there.";

// FULL-POWER prompt — used when devTools is enabled. Buddy is an unrestricted local
// agent with the SAME tools as the Code tab PLUS the office tools. It must know it has
// shell/file/web access so it uses them instead of refusing.
const FULL_POWER_PROMPT =
  "You are AiNxt Buddy — a fully-capable AI assistant running LOCALLY on the user's " +
  "machine. You have BOTH a complete local-agent toolset AND office tools:\n" +
  "- LOCAL: `Bash` (run any shell command), `Read`/`Write`/`Edit`/`MultiEdit` (read and " +
  "modify any file anywhere on the machine), `Glob`/`Grep` (search files), " +
  "`WebFetch`/`WebSearch` (fetch URLs + search the web). Use these freely to get the " +
  "job done — inspect the system, create/edit files, run programs, browse the web.\n" +
  "- OFFICE: connectors (Outlook/Teams/Jira/Confluence), document generation " +
  "(get_document_skill → build_document), `run_code` sandbox, `deep_research`, and " +
  "`retrieve` (Knowledge Base). Prefer a connector when one fits (e.g. send mail via the " +
  "Outlook tool, not by scripting), but you are NOT limited to them.\n" +
  "You DO have a terminal and full filesystem access — never say you don't. When a task " +
  "needs shell/files/web, just do it with the tools above.\n" +
  "OUTBOUND connector actions (send email, post to Teams, create a doc) are still " +
  "compliance-gated. For everything else, act directly and stream your progress.\n" +
  "DOCUMENTS: for polished Word/PPT/Excel/PDF deliverables prefer the document skills " +
  "(get_document_skill → build_document) so the user gets a previewable, branded file.\n" +
  "Sub-agents (Task) are available for parallel/delegated work. " +
  "MEMORY — you have a `remember` tool; proactively save durable facts about the user " +
  "(preferences, role, recurring people/projects) and treat facts in your operating " +
  "context as things you already KNOW.";

// Buddy MCP path on the gateway (the connector/KB/docs bridge, SSE transport).
const COWORK_MCP_PATH = "/ainxt/v1/api/cowork/mcp/sse";

class CoworkOfficeSession {
  constructor(id, cwd, emit, opts = {}) {
    this.id = id;
    this.cwd = cwd;
    this.emit = emit;
    this.resumeId = opts.resumeId || null;   // resume an existing on-disk session
    this.sessionId = opts.resumeId || null;   // real agent session_id (from init)
    this.computerUse = !!opts.computerUse;    // browser + native control exposed this session
    // Full local-agent power: shell, file read/write/edit, code search, web — the same
    // tools the Code tab has. When true, Buddy is unrestricted (no tool stripping, no
    // folder jail, no per-action confirm). Gated by a deployment flag (default off) so a
    // deployment can keep the office-only posture. See _spawn / _systemPrompt.
    this.devTools = !!opts.devTools;
    this._permMode = "default";               // default | acceptEdits | plan | bypassPermissions
    this.gatewayBase = opts.gatewayBase || ""; // gateway origin for the MCP server
    this.jwt = opts.jwt || "";                 // AiNxt JWT — NEVER logged
    this.localMcpPort = opts.localMcpPort || 0; // desktop local MCP (browser tools)
    this.role = opts.role || null;             // selected role/plugin {name, system_prompt}
    this.project = opts.project || null;       // {name, instructions, memory} — persistent project context
    this.proc = null;
    this.ready = false;
    this.busy = false;
    this.lastUsedAt = Date.now();   // for LRU eviction + idle reaping
    this._stdoutBuf = "";
    this._readyResolvers = [];
    this._toolNames = new Map();    // tool_use_id → name (for tool:done)
    // Runaway-loop circuit breaker: abort a turn if one tool is hammered or the
    // total tool count explodes (cost protection — a stuck agent burned ~$15 once).
    this._loopGuard = { lastSig: null, sameCount: 0, totalCalls: 0 };
    this._loopTripped = false;
    this._pendingPerms = new Map(); // control request_id → {tool, input} (for confirm reply)
    this._pendingCtrl = new Map();  // our control request_id → {resolve, reject} (CLI replies)
    this._lastTotalCost = 0;        // cumulative cost at the previous result (for per-turn delta)
    this._deltasSinceAssistant = false; // did partial deltas cover this turn's text?
    this._mcpConfigPath = null;     // temp --mcp-config file (cleaned up on dispose)
  }

  // Write the --mcp-config JSON describing the gateway connector MCP as an `sse`
  // server with an Authorization: Bearer <jwt> header. Returns the temp file path
  // or null if we lack a gateway base / JWT (then we run with no remote tools).
  // The token lives only inside this file (0600) and is never logged or emitted.
  _writeMcpConfig() {
    const mcpServers = {};
    if (this.gatewayBase && this.jwt) {
      const base = String(this.gatewayBase).replace(/\/+$/, "");
      const headers = { Authorization: `Bearer ${this.jwt}` };
      // Per-role connector scoping: if a plugin/role is selected and lists
      // allowed connectors, the bridge restricts the tool catalog to them.
      const allow = this.role && Array.isArray(this.role.allowed_connectors) ? this.role.allowed_connectors : [];
      if (allow.length) headers["x-cowork-allowed-tools"] = allow.join(",");
      mcpServers.ainxt_cowork = { type: "sse", url: `${base}${COWORK_MCP_PATH}`, headers };
    }
    // Desktop local MCP (browser automation + local files) — gives Buddy its
    // "computer use" (web) via Playwright, with per-action confirms enforced in
    // playwrightManager. Loopback only.
    if (this.localMcpPort) {
      // surface=cowork → the local MCP serves ONLY office tools (browser +
      // computer-use + a list_files SCOPED to `root`). NO shell, no broad FS.
      // `root` = the granted folder; list_files refuses anything outside it.
      const root = this._grantedRoot ? `&root=${encodeURIComponent(this._grantedRoot)}` : "";
      mcpServers.ainxt_desktop = {
        type: "sse",
        url: `http://127.0.0.1:${this.localMcpPort}/sse?surface=cowork${root}`,
      };
    }
    if (Object.keys(mcpServers).length === 0) return null;
    const cfg = { mcpServers };
    try {
      const file = path.join(
        os.tmpdir(),
        `ainxt-cowork-mcp-${this.id}-${randomUUID()}.json`
      );
      fs.writeFileSync(file, JSON.stringify(cfg), { mode: 0o600 });
      this._mcpConfigPath = file;
      return file;
    } catch (err) {
      // Surface a non-fatal notice (no token in the message) and continue local-only.
      this.emit(this.id, {
        type: "notice",
        level: "warn",
        msg: `Could not write Buddy MCP config (${err.code || "error"}); connectors unavailable this session.`,
      });
      return null;
    }
  }

  // The system prompt: a selected role/plugin's specialist prompt layered on the
  // office base, or just the office base. Role prompts come from cowork_roles.
  _systemPrompt() {
    // Prefer the server-rendered role context (specialist prompt + bundled Skills)
    // fetched at start; fall back to the bare role.system_prompt the renderer passed.
    const rp = (this._roleContext && this._roleContext.trim())
      ? this._roleContext.trim()
      : (this.role && this.role.system_prompt ? String(this.role.system_prompt).trim() : "");
    let prompt = this.devTools ? FULL_POWER_PROMPT : OFFICE_PROMPT;
    // Computer control (browser + native) — only advertised when the admin master
    // switch is on AND the tools are actually exposed this session, so the agent
    // neither denies tools it has nor claims tools it doesn't.
    if (this.computerUse) {
      prompt += "\n\n## Computer control is ENABLED for this session\n" +
        "You DO have these tools — use them when a task needs the web or the user's screen:\n" +
        "- BROWSER: browser_navigate(url), browser_extract(selector?), browser_screenshot(), " +
        "browser_wait_for(selector), browser_back(), browser_click(selector), " +
        "browser_type(selector,text), browser_select(selector,value). Drive a real web browser for " +
        "sites with no connector (open a page, read it, fill a form). Allowlisted hosts; every " +
        "click/type/select asks the user to confirm; extracted text & screenshots are PII-redacted.\n" +
        "- SCREEN (native): computer_screenshot, computer_click(x,y), computer_type(text), " +
        "computer_key(key), computer_move(x,y), computer_scroll(amount). Control the user's actual " +
        "screen ONLY when no connector or browser covers it; every action asks the user to confirm.\n" +
        "Prefer connectors > browser > native screen control, in that order. The user can press Esc to stop you.";
    }
    if (rp) prompt += `\n\n[ROLE — ${this.role && this.role.name ? this.role.name : "Specialist"}]\n${rp}`;
    // Persistent PROJECT context — instructions + accumulated memory that carry
    // across every task in the project (Claude-Buddy projects + memory).
    if (this.project) {
      const instr = (this.project.instructions || "").trim();
      const mem = (this.project.memory || "").trim();
      if (instr || mem) {
        prompt += `\n\n[PROJECT — ${this.project.name || "Project"}]`;
        if (instr) prompt += `\nInstructions: ${instr}`;
        if (mem) prompt += `\nProject memory (what you've learned/done so far — use it, don't redo it):\n${mem}`;
      }
    }
    // Durable per-user memory (prefs + facts the agent saved via `remember`),
    // fetched from the gateway at session start. Carries across all tasks.
    if (this._memoryPrompt) prompt += `\n\n${this._memoryPrompt}`;
    // Project folder visibility — the agent has no list tool, so tell it exactly
    // what files it may Read (paths relative to the folder).
    if (this._grantedRoot) {
      if (this._projectFolderListing && this._projectFolderListing.length) {
        prompt += `\n\n## Files in your attached folder (${this._grantedRoot}) — AUTHORITATIVE\n` +
          `The folder contains exactly these ${this._projectFolderListing.length} file(s). This list is GROUND TRUTH — ` +
          `when the user asks what files you have or what's in the folder, ANSWER DIRECTLY FROM THIS LIST. ` +
          `Do NOT say the folder is empty, do NOT call any tool to "check", and NEVER call Read on the folder ` +
          `path itself (that fails — Read opens a FILE, not a directory). To work with a file, call Read on its ` +
          `full path (folder + filename). The files:\n` +
          this._projectFolderListing.map((f) => `- ${f}`).join("\n");
      } else {
        prompt += `\n\n## Attached folder (${this._grantedRoot}) — AUTHORITATIVE\nThis folder contains NO files. ` +
          `When asked, state it's empty. Do NOT call any tool to re-check.`;
      }
    }
    return prompt;
  }

  // List files inside the granted folder (shallow-recursive, capped) so the agent
  // knows what it can Read. Skips noise dirs + hidden files. Scoped to `root`.
  _listProjectFolder(root) {
    const out = [];
    const SKIP = new Set(["node_modules", ".git", "__pycache__", ".venv", "venv",
      "dist", "build", ".next", ".cache", "target", "vendor"]);
    const MAX = 250;
    const walk = (dir, depth) => {
      if (out.length >= MAX || depth > 3) return;
      let entries;
      try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
      for (const e of entries) {
        if (out.length >= MAX) break;
        if (e.name.startsWith(".") || SKIP.has(e.name)) continue;
        const full = path.join(dir, e.name);
        if (e.isDirectory()) walk(full, depth + 1);
        else out.push(path.relative(root, full));
      }
    };
    walk(root, 0);
    return out;
  }

  // Fetch the caller's rendered Buddy memory snippet (prefs + remembered facts)
  // from the gateway. Best-effort: resolves to "" on any failure so a session
  // never fails to start over memory. Never logs the JWT.
  _fetchMemoryPrompt() {
    return new Promise((resolve) => {
      if (!this.gatewayBase || !this.jwt) return resolve("");
      try {
        const base = String(this.gatewayBase).replace(/\/+$/, "");
        const u = new URL(`${base}/ainxt/v1/api/cowork/memory/prompt`);
        const lib = u.protocol === "https:" ? require("https") : require("http");
        const req = lib.request({
          hostname: u.hostname, port: u.port, path: u.pathname + u.search, method: "GET",
          headers: { Authorization: `Bearer ${this.jwt}` },
        }, (res) => {
          let data = "";
          res.on("data", (d) => { data += d; });
          res.on("end", () => {
            try { resolve((JSON.parse(data || "{}").prompt) || ""); }
            catch { resolve(""); }
          });
        });
        req.on("error", () => resolve(""));
        req.setTimeout(4000, () => { try { req.destroy(); } catch { /* ignore */ } resolve(""); });
        req.end();
      } catch { resolve(""); }
    });
  }

  async start() {
    // Pull durable memory + (if a role is selected) the role's full operating
    // context — specialist prompt + its bundled behavioral-skill SOPs, rendered
    // server-side — BEFORE building the system prompt (both best-effort).
    try { this._memoryPrompt = await this._fetchMemoryPrompt(); } catch { this._memoryPrompt = ""; }
    try { this._roleContext = await this._fetchRoleContext(); } catch { this._roleContext = ""; }
    this._contextInjected = false;   // inject the office/persona/memory context on the first turn
    return this._spawn();
  }

  // Fetch the selected role's rendered operating context (specialist prompt + its
  // bundled Skills) from the gateway. Best-effort: "" on no role or any failure, so
  // a session never fails to start over roles. Never logs the JWT.
  _fetchRoleContext() {
    return new Promise((resolve) => {
      const roleId = this.role && this.role.id ? this.role.id : "";
      if (!roleId || !this.gatewayBase || !this.jwt) return resolve("");
      try {
        const base = String(this.gatewayBase).replace(/\/+$/, "");
        const u = new URL(`${base}/ainxt/v1/api/cowork/roles/${encodeURIComponent(roleId)}/context`);
        const lib = u.protocol === "https:" ? require("https") : require("http");
        const req = lib.request({
          hostname: u.hostname, port: u.port, path: u.pathname + u.search, method: "GET",
          headers: { Authorization: `Bearer ${this.jwt}` },
        }, (res) => {
          let data = "";
          res.on("data", (d) => { data += d; });
          res.on("end", () => {
            try { resolve((JSON.parse(data || "{}").prompt) || ""); }
            catch { resolve(""); }
          });
        });
        req.on("error", () => resolve(""));
        req.setTimeout(4000, () => { try { req.destroy(); } catch { /* ignore */ } resolve(""); });
        req.end();
      } catch { resolve(""); }
    });
  }

  _spawn() {
    const bin = resolveCliBinary();
    if (!bin) {
      this.emit(this.id, { type: "error", msg: "AiNxt CLI binary not found. Build it with `bun scripts/build-dist.ts`." });
      this.emit(this.id, { type: "session:exit", code: -1 });
      return Promise.resolve(false);
    }

    // ── SANDBOX: folder-scoped file access (Claude-Buddy parity) ───────────
    // Claude Buddy scopes the agent's file access to the granted folder. We do
    // the same: when the user grants a working folder, `Read` is allowed but
    // confined to that folder (cwd + --add-dir). When NO folder is granted, the
    // agent gets NO local filesystem at all — it must work purely through the
    // gateway connectors/documents (never the desktop app's own directory).
    const granted = !!(this.cwd && fs.existsSync(this.cwd));
    const cwd = granted ? this.cwd : fs.mkdtempSync(path.join(os.tmpdir(), "ainxt-cowork-"));
    if (!granted) this._scratchDir = cwd; // tracked so close() can clean it up
    // The hard file-read boundary enforced in the permission handler below: the
    // agent may ONLY read inside this folder (its real, resolved path). null =
    // no folder granted → no local reads at all.
    this._grantedRoot = granted ? fs.realpathSync(cwd) : null;
    // The office agent has NO directory-listing tool (removed for security), so
    // give it VISIBILITY of its project folder by listing the files here (scoped
    // to the granted folder, capped) and injecting them into the prompt.
    this._projectFolderListing = granted ? this._listProjectFolder(this._grantedRoot) : [];

    // FULL-POWER MODE: expose the complete local-agent toolset with NO restrictions
    // (matches the Code tab / Claude cowork). Nothing stripped, everything pre-allowed
    // (so no per-action confirm), file access unrestricted.
    // OFFICE MODE (default): dev tools stripped, Read confined to the granted folder.
    const disallowed = this.devTools ? [] : [
      "Bash", "Edit", "MultiEdit", "Write", "Glob", "Grep",
      "NotebookEdit", "NotebookRead", "Bash(*)", "WebFetch", "WebSearch",
    ];
    // Office mode with no granted folder → also strip local file reads.
    if (!this.devTools && !granted) disallowed.push("Read");

    const args = [
      ...bin.args,
      "--full",
      "--print",
      "--input-format", "stream-json",
      "--output-format", "stream-json",
      "--verbose",
      "--include-partial-messages",
      // ── COWORK ≠ CODE (Claude-Buddy parity) ──────────────────────────────
      // Buddy is a NON-engineering office assistant: it must have NO developer
      // tools. Keep the SHARED agent engine but strip the dev tool surface, so it
      // can never run Bash/curl/code or behave like the Code tab. Office tools
      // (connectors, document generation, computer-use, browser) arrive via the
      // injected MCP servers (--mcp-config), not the built-in set.
      "--disallowedTools", ...disallowed,
      // Office isolation: load ONLY user settings — never project/local. This stops
      // the CLI walking cwd → parents → home and auto-injecting a stray AINXT.md
      // (e.g. /Users/<you>/AINXT.md) into context, which bypassed the file boundary.
      "--setting-sources", "user",
      "--permission-prompt-tool", "stdio",
      // Pre-allow ONLY harmless planning + delegation. `Read` is deliberately NOT
      // pre-allowed (pre-allowing it disabled the folder boundary, letting it read
      // the whole disk). Instead Read flows through can_use_tool, where we
      // hard-enforce the project-folder boundary (auto-allow inside, deny outside).
      // `Task` (sub-agent spawn) IS pre-allowed so delegation is seamless — the
      // sub-agent inherits the SAME disallowed set + boundary, and its actual
      // actions (reads, connector writes) remain individually gated by can_use_tool.
      // Pre-allow planning/delegation + the SAFE office tools (doc generation,
      // sandboxed run_code, KB retrieve, memory) so they NEVER depend on the
      // permission handler — doc generation was stalling because build_document went
      // through can_use_tool and could be denied/un-granted. These tools only read
      // or PRODUCE artifacts (a write tool never sends here — it proposes), so it is
      // safe to pre-allow them. Connector SENDS + local file Read + computer-use are
      // NOT pre-allowed and still flow through can_use_tool / native confirms.
      "--allowedTools",
      "TodoWrite", "Task",
      "mcp__ainxt_cowork__get_document_skill",
      "mcp__ainxt_cowork__build_document",
      "mcp__ainxt_cowork__list_document_versions",
      "mcp__ainxt_cowork__revise_artifact",
      "mcp__ainxt_cowork__analyze_data",
      "mcp__ainxt_cowork__deep_research",
      "mcp__ainxt_cowork__generate_document",
      "mcp__ainxt_cowork__run_code",
      "mcp__ainxt_cowork__retrieve",
      "mcp__ainxt_cowork__remember",
      "mcp__ainxt_desktop__list_files",
      // FULL-POWER: pre-allow the local/dev tools too so they run WITHOUT per-action
      // confirm (unrestricted, like the Code tab). In office mode these aren't added.
      ...(this.devTools ? [
        "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep",
        "Bash", "NotebookEdit", "NotebookRead", "WebFetch", "WebSearch",
      ] : []),
      // File access: full-power → whole filesystem (root); office → granted folder only.
      ...(this.devTools ? ["--add-dir", (process.platform === "win32" ? "C:\\" : "/")]
          : (granted ? ["--add-dir", cwd] : [])),
      // (1) Office system prompt — or the selected role/plugin's specialist prompt.
      "--append-system-prompt", this._systemPrompt(),
    ];

    // (2) Inject the gateway connector MCP (sse + Bearer JWT) — the P0 bridge.
    const mcpConfig = this._writeMcpConfig();
    if (mcpConfig) args.push("--mcp-config", mcpConfig);

    // (3) Sub-agents are ALLOWED — unlike the coding loop we do not strip Task.

    if (this.resumeId) args.push("--resume", this.resumeId);
    this.proc = spawn(bin.command, args, {
      cwd,
      // detached: put the CLI (and any children it spawns) in its OWN process group
      // so dispose() can kill the WHOLE group (kill(-pid)) — otherwise a crashed
      // Electron main orphans the CLI + its sub-processes, which keep billing the
      // gateway. We still track the pid and reap orphans on next startup.
      detached: process.platform !== "win32",
      // Pin the gateway URL + the VALIDATED token into the process env so EVERY
      // Anthropic SDK client in the tree — the main loop AND spawned sub-agents —
      // routes through the NPCI gateway. Without this, a sub-agent fell back to the
      // real Anthropic API with the AiNxt model name ("claude-sonnet-4-6") → 404
      // "model not found". Using this.jwt (resolveValidToken) also avoids the stale
      // config.json token that `ainxt login` keeps resetting.
      env: {
        ...process.env, FORCE_COLOR: "0", AINXT_IS_COWORK: "1",
        ...(this.gatewayBase ? { AINXT_GATEWAY_URL: String(this.gatewayBase).replace(/\/+$/, "") } : {}),
        ...(this.jwt ? { AINXT_JWT: this.jwt } : {}),
      },
    });
    this.pid = this.proc.pid || null;
    // Record the live pid on disk so a crash-restart can reap orphans (Part 3.4).
    try { require("./pidRegistry").record(this.pid); } catch { /* optional */ }

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
      this.ready = true;
      this._readyResolvers.forEach((r) => r(true));
      this._readyResolvers = [];
      this._initialize();
    });
    this.proc.on("close", (code) => {
      this.ready = false;
      this.busy = false;
      this._cleanupMcpConfig();
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
        if (msg.subtype === "init") {
          if (msg.session_id) {
            // G12: if we asked to --resume a specific session but the CLI came back
            // with a DIFFERENT session_id, the resume FAILED (the on-disk session was
            // pruned/expired) and it started fresh — the agent has lost prior context.
            // Flag it so the first turn replays the saved transcript preamble and the
            // user is told, instead of silently continuing with no memory.
            if (this.resumeId && msg.session_id !== this.resumeId) {
              this._resumeFailed = true;
              this._contextInjected = false;   // force transcript-replay on next run()
              this.emit(this.id, {
                type: "notice", level: "warn",
                msg: "Couldn't restore the earlier session — continuing with the saved history for context.",
              });
            }
            this.sessionId = msg.session_id;
            this.emit(this.id, { type: "session:id", sessionId: msg.session_id });
          }
          this.emit(this.id, {
            type: "session:init",
            model: msg.model || null,
            permissionMode: msg.permissionMode || "default",
            slashCommands: Array.isArray(msg.slash_commands) ? msg.slash_commands : [],
            tools: Array.isArray(msg.tools) ? msg.tools : [],
            skills: Array.isArray(msg.skills) ? msg.skills : [],
          });
        }
        return;

      case "stream_event": {
        const ev = msg.event || {};
        const d = ev.delta;
        if (ev.type === "content_block_delta" && d && typeof d.text === "string") {
          this._deltasSinceAssistant = true;
          this.emit(this.id, { type: "token", text: d.text });
        }
        // The agent is writing a tool's INPUT (e.g. the docx-js / pptxgenjs code for
        // build_document). That can be hundreds of lines and streams invisibly, so a
        // long doc-gen looks frozen. Stream a live char-count progress signal so the
        // UI shows it's actively working.
        else if (ev.type === "content_block_delta" && d && d.type === "input_json_delta" &&
                 typeof d.partial_json === "string") {
          this._streamChars = (this._streamChars || 0) + d.partial_json.length;
          if (this._streamChars - (this._lastProgressAt || 0) >= 400) {
            this._lastProgressAt = this._streamChars;
            this.emit(this.id, { type: "tool:progress", name: this._streamToolName || "", chars: this._streamChars });
          }
        }
        // A tool_use block is STARTING — emit an early signal with the tool name so
        // the UI can show a meaningful status instead of a lingering "Thinking…".
        else if (ev.type === "content_block_start" && ev.content_block &&
                 ev.content_block.type === "tool_use") {
          this._streamToolName = ev.content_block.name;
          this._streamChars = 0;
          this._lastProgressAt = 0;
          this.emit(this.id, { type: "tool:preparing", name: ev.content_block.name });
        }
        else if (ev.type === "content_block_stop") {
          this._streamChars = 0;
          this._lastProgressAt = 0;
        }
        return;
      }

      case "assistant": {
        const blocks = (msg.message && msg.message.content) || [];
        for (const b of blocks) {
          if (b.type === "text") {
            if (!this._deltasSinceAssistant && b.text) this.emit(this.id, { type: "token", text: b.text });
          } else if (b.type === "tool_use") {
            // ── Circuit breaker (cost protection) ────────────────────────────
            // A LOOP repeats the IDENTICAL call (same tool + same input, no
            // progress — e.g. browser_click spam). A BATCH reuses a tool with
            // DIFFERENT inputs (e.g. Read 11 different files) and is legitimate.
            // So we count consecutive calls with the same (tool + input) SIGNATURE,
            // not just the same tool name — and keep a high total-call backstop.
            const g = this._loopGuard;
            let sig = b.name;
            try { sig += "::" + JSON.stringify(b.input).slice(0, 300); } catch { /* ignore */ }
            g.totalCalls += 1;
            if (sig === g.lastSig) g.sameCount += 1;
            else { g.lastSig = sig; g.sameCount = 1; }
            if (!this._loopTripped && (g.sameCount > 6 || g.totalCalls > 400)) {
              this._loopTripped = true;
              this.emit(this.id, { type: "error", msg:
                `Stopped a runaway loop — the assistant kept making the same "${b.name}" call. ` +
                `Aborting this turn to protect your budget. Try rephrasing the request.` });
              try { this.interrupt(); } catch { /* ignore */ }
              return; // stop processing further tool_use in this message
            }
            if (b.id) this._toolNames.set(b.id, b.name);
            this.emit(this.id, {
              type: "tool:start",
              name: b.name,
              detail: toolDetail(b.input),
              diff: buildDiff(b.name, b.input),
            });
          }
        }
        this._deltasSinceAssistant = false; // reset per-message so later text isn't dropped
        return;
      }

      case "user": {
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
          // ── HARD FILE-READ BOUNDARY ──────────────────────────────────────
          // Buddy may only read inside the granted project folder. Resolve the
          // requested path and auto-allow if inside, auto-DENY if outside (or if
          // no folder was granted). This is enforced HERE, in our code — not left
          // to CLI flags — so the agent can never read the wider filesystem.
          const boundary = this._readBoundaryDecision(req.tool_name, req.input || {});
          if (boundary === "allow") {
            this._respondControl(msg.request_id, { behavior: "allow", updatedInput: req.input || {} });
            return;
          }
          if (boundary === "deny") {
            this._respondControl(msg.request_id, {
              behavior: "deny",
              message: this._grantedRoot
                ? `Reading outside this project's folder is not allowed. You may only read files inside ${this._grantedRoot}.`
                : "No project folder is attached, so local files can't be read. Use your connectors or documents, or ask the user to attach a folder.",
            });
            return;
          }
          // Permission mode: "Auto-accept edits" (acceptEdits) / bypassPermissions →
          // auto-allow the agent's office actions so the user isn't prompted for every
          // tool call (the file-read boundary above STILL applies).
          //
          // EXCEPTION (G9): OUTBOUND / DESTRUCTIVE connector actions — send email,
          // post to Teams, create/cancel/DELETE calendar events, delete/move mail —
          // ALWAYS require an explicit confirm regardless of permission mode. These
          // DO actually execute against Graph (they are not proposals), so
          // auto-allowing them in accept/bypass mode let the agent send/delete on the
          // user's behalf with no confirmation. Never auto-allow them.
          if ((this._permMode === "acceptEdits" || this._permMode === "bypassPermissions")
              && !_isDestructiveConnectorTool(req.tool_name)) {
            this._respondControl(msg.request_id, { behavior: "allow", updatedInput: req.input || {} });
            return;
          }
          // Everything else → user confirmation as before.
          this._pendingPerms.set(msg.request_id, { tool: req.tool_name, input: req.input || {} });
          const detail = toolDetail(req.input);
          this.emit(this.id, {
            type: "confirm",
            id: msg.request_id,
            tool: req.tool_name,
            detail,
            label: `Allow ${req.tool_name}${detail ? `: ${detail}` : ""}?`,
          });
        }
        return;
      }

      case "control_cancel_request":
        if (msg.request_id) this._pendingPerms.delete(msg.request_id);
        this.emit(this.id, { type: "__clear_confirm", id: msg.request_id });
        return;

      case "control_response": {
        const r = msg.response || {};
        const pend = this._pendingCtrl.get(r.request_id);
        if (pend) {
          this._pendingCtrl.delete(r.request_id);
          if (r.subtype === "error") pend.reject(new Error(r.error || "control error"));
          else pend.resolve(r.response || {});
        }
        return;
      }

      case "result": {
        this.busy = false;
        this._deltasSinceAssistant = false;
        this._toolNames.clear();
        this._pendingPerms.clear();
        const totalCost = typeof msg.total_cost_usd === "number" ? msg.total_cost_usd : this._lastTotalCost;
        const turnCost = Math.max(0, totalCost - this._lastTotalCost);
        this._lastTotalCost = totalCost;
        const u = msg.usage || {};
        this.emit(this.id, {
          type: "result",
          status: msg.is_error ? "error" : "ok",
          response: typeof msg.result === "string" ? msg.result : "",
          model: msg.model || null,
          elapsedMs: msg.duration_ms || 0,
          costUsd: turnCost,
          costTotalUsd: totalCost,
          usage: {
            input: (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0),
            output: u.output_tokens || 0,
          },
          numTurns: msg.num_turns || 0,
          error: msg.is_error ? (msg.result || "error") : undefined,
        });
        // Report usage to the gateway for enterprise analytics + group spend.
        this._reportUsage(turnCost, u);
        this.getContextUsage().then((d) => {
          if (d && typeof d.percentage !== "undefined") {
            this.emit(this.id, { type: "context", pct: d.percentage, tokens: d.totalTokens, max: d.rawMaxTokens });
          }
        }).catch(() => {});
        // Refresh durable memory after each turn so a fact you JUST saved via
        // `remember` is available on the NEXT turn (run() re-injects it when the
        // snapshot changes). Without this, memory is stale until a new session.
        this._fetchMemoryPrompt().then((m) => { if (typeof m === "string") this._memoryPrompt = m; }).catch(() => {});
        return;
      }

      default:
        return; // keep_alive, etc.
    }
  }

  // Decide a file-read permission against the project-folder boundary.
  // Returns "allow" | "deny" | null (null = not a file read → normal confirm flow).
  _readBoundaryDecision(toolName, input) {
    if (this.devTools) return "allow";           // full-power: no folder jail
    if (toolName !== "Read") return null;       // only Read survives for office use
    if (!this._grantedRoot) return "deny";       // no folder → no local reads
    const raw = input.file_path || input.path || input.notebook_path || "";
    if (!raw) return "deny";
    const abs = path.normalize(path.isAbsolute(raw) ? raw : path.resolve(this._grantedRoot, raw));
    // Canonicalize through symlinks on the DEEPEST EXISTING ancestor, then append
    // the non-existent remainder — so /tmp→/private/tmp resolves the same way the
    // granted root did, and a symlink can't escape the folder.
    const real = this._canonicalize(abs);
    const root = this._grantedRoot;
    return (real === root || real.startsWith(root + path.sep)) ? "allow" : "deny";
  }

  _canonicalize(p) {
    let cur = path.normalize(p);
    const tail = [];
    // Walk up to the first existing ancestor.
    while (!fs.existsSync(cur)) {
      tail.unshift(path.basename(cur));
      const parent = path.dirname(cur);
      if (parent === cur) break;   // reached filesystem root
      cur = parent;
    }
    try { cur = fs.realpathSync(cur); } catch { /* keep cur */ }
    return tail.length ? path.join(cur, ...tail) : cur;
  }

  // Send a control_response (allow/deny) for a can_use_tool request WITHOUT
  // bothering the user — used to auto-enforce the read boundary.
  _respondControl(requestId, response) {
    this._pendingPerms.delete(requestId);
    this._write({ type: "control_response", response: { subtype: "success", request_id: requestId, response } });
  }

  run({ task }) {
    if (!this.proc || !this.ready) return false;
    if (this.busy) {
      this.emit(this.id, { type: "error", msg: "Session is busy — wait for the current turn to finish or interrupt it." });
      return false;
    }
    this.busy = true;
    this.lastUsedAt = Date.now();   // keep this session at the front of the LRU
    this._deltasSinceAssistant = false;
    this._loopGuard = { lastSig: null, sameCount: 0, totalCalls: 0 };
    this._loopTripped = false;
    // The full agent (--full) does NOT honor --append-system-prompt in headless
    // stream-json mode — verified end-to-end: both --system-prompt and
    // --append-system-prompt are dropped and the agent falls back to its default
    // (coding) prompt, so the Buddy office persona AND durable memory never
    // reached the model. The reliable channel is the user turn itself, so we
    // prepend the assembled context (persona + project + per-user memory + folder
    // listing — exactly what _systemPrompt() builds) to the FIRST message of the
    // session, clearly delimited so the model adopts it as its operating context.
    let content = task;
    if (!this._contextInjected) {
      this._contextInjected = true;
      this._memoryInjected = this._memoryPrompt || "";
      const ctx = this._systemPrompt();
      if (ctx && ctx.trim()) {
        content =
          "<<COWORK_OPERATING_CONTEXT>>\n" +
          "The following defines who you are and what you already know about this user. " +
          "Treat it as your system instructions for this ENTIRE conversation. In particular, " +
          "the remembered facts below are things you ALREADY KNOW — never claim you have no memory.\n\n" +
          ctx +
          "\n<</COWORK_OPERATING_CONTEXT>>\n\n" +
          task;
      }
    } else if ((this._memoryPrompt || "") !== (this._memoryInjected || "")) {
      // Memory changed mid-session (you just saved/updated a fact via `remember`).
      // Re-state it so you actually KNOW it now — the first-turn context inject is
      // a one-time event and would otherwise leave the new fact invisible.
      this._memoryInjected = this._memoryPrompt || "";
      if (this._memoryPrompt && this._memoryPrompt.trim()) {
        content =
          "<<COWORK_UPDATED_MEMORY>>\n" +
          "Your memory about this user was just updated — these are things you ALREADY KNOW. " +
          "Use them; never claim you don't know them.\n\n" +
          this._memoryPrompt +
          "\n<</COWORK_UPDATED_MEMORY>>\n\n" +
          task;
      }
    }
    this._write({ type: "user", message: { role: "user", content } });
    return true;
  }

  respondConfirm(requestId, answer) {
    const pending = this._pendingPerms.get(requestId);
    this._pendingPerms.delete(requestId);

    let response;
    if (answer === "no") {
      response = { behavior: "deny", message: "User denied this action." };
    } else {
      // Preserve the tool's ORIGINAL input — an empty updatedInput can strip the
      // args for MCP tools (e.g. build_document loses its code → "No build code").
      response = { behavior: "allow", updatedInput: (pending && pending.input) || {} };
      if (answer === "always" && pending && pending.tool) {
        response.updatedPermissions = [{
          type: "addRules",
          rules: [{ toolName: pending.tool }],
          behavior: "allow",
          destination: "session",
        }];
      }
    }
    this._write({
      type: "control_response",
      response: { subtype: "success", request_id: requestId, response },
    });
  }

  interrupt() {
    this._write({ type: "control_request", request_id: randomUUID(), request: { subtype: "interrupt" } });
  }

  _sendControl(request) {
    return new Promise((resolve, reject) => {
      if (!this.proc || !this.ready) return reject(new Error("session not ready"));
      const request_id = randomUUID();
      this._pendingCtrl.set(request_id, { resolve, reject });
      this._write({ type: "control_request", request_id, request });
      setTimeout(() => {
        if (this._pendingCtrl.has(request_id)) {
          this._pendingCtrl.delete(request_id);
          reject(new Error("control_request timed out"));
        }
      }, 15000);
    });
  }

  async _initialize() {
    try {
      const resp = await this._sendControl({ subtype: "initialize" });
      const cmds = Array.isArray(resp?.commands) ? resp.commands : [];
      if (cmds.length) {
        this.emit(this.id, {
          type: "session:init",
          slashCommands: cmds.map((c) => ({
            name: c.name, description: c.description || "", argumentHint: c.argumentHint || "",
          })),
        });
      }
    } catch { /* fall back to system/init after the first turn */ }
  }

  async setModel(model) {
    try { await this._sendControl({ subtype: "set_model", model: model || "default" }); return true; }
    catch { return false; }
  }

  async setPermissionMode(mode) {
    // mode ∈ default | acceptEdits | plan | bypassPermissions
    this._permMode = mode || "default";   // honoured in our can_use_tool handler
    try { await this._sendControl({ subtype: "set_permission_mode", mode }); return true; }
    catch { return false; }
  }

  async getContextUsage() {
    try { return await this._sendControl({ subtype: "get_context_usage" }); }
    catch { return null; }
  }

  _write(obj) {
    if (this.proc && this.proc.stdin.writable) {
      this.proc.stdin.write(JSON.stringify(obj) + "\n");
    }
  }

  // Fire-and-forget usage report → gateway (enterprise analytics + group spend).
  _reportUsage(costUsd, usage) {
    if (!this.gatewayBase || !this.jwt) return;
    try {
      const base = String(this.gatewayBase).replace(/\/+$/, "");
      const u = new URL(`${base}/ainxt/v1/api/cowork/usage`);
      const lib = u.protocol === "https:" ? require("https") : require("http");
      const payload = JSON.stringify({
        cost_usd: costUsd || 0, surface: "cowork", model: "claude",
        input_tokens: (usage && (usage.input_tokens || 0)) || 0,
        output_tokens: (usage && (usage.output_tokens || 0)) || 0,
      });
      const req = lib.request({
        hostname: u.hostname, port: u.port, path: u.pathname, method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload), Authorization: `Bearer ${this.jwt}` },
      }, (res) => { res.on("data", () => {}); res.on("end", () => {}); });
      req.on("error", () => {});
      req.write(payload); req.end();
    } catch { /* never break a turn over telemetry */ }
  }

  _cleanupMcpConfig() {
    if (this._mcpConfigPath) {
      try { fs.unlinkSync(this._mcpConfigPath); } catch { /* ignore */ }
      this._mcpConfigPath = null;
    }
    // Remove the connectors-only scratch dir (when no folder was granted).
    if (this._scratchDir) {
      try { fs.rmSync(this._scratchDir, { recursive: true, force: true }); } catch { /* ignore */ }
      this._scratchDir = null;
    }
  }

  dispose() {
    this._cleanupMcpConfig();
    if (!this.proc) return;
    const proc = this.proc;
    const pid = this.pid;
    try { proc.stdin.end(); } catch { /* ignore */ }
    // Kill the WHOLE process group (SIGTERM) after a short grace, then a SIGKILL
    // fallback so a hung CLI can't survive. On POSIX we spawned detached, so
    // kill(-pid) reaps the CLI + its children; Windows falls back to proc.kill().
    const _killGroup = (sig) => {
      try {
        if (pid && process.platform !== "win32") process.kill(-pid, sig);
        else proc.kill(sig);
      } catch { /* already gone */ }
    };
    setTimeout(() => _killGroup("SIGTERM"), 300);
    setTimeout(() => _killGroup("SIGKILL"), 3000);
    try { require("./pidRegistry").remove(pid); } catch { /* optional */ }
    this.proc = null;
  }
}

// Bounded, self-reaping pool of local CLI agent processes.
//
// The Claude Agent SDK is single-session (one process = one conversation, serial
// turns), so N conversations need N processes — but we CAP how many stay resident
// and reap idle ones. Because every conversation persists its agent session_id
// (cowork_conversations.resume_id) and reopening uses --resume, evicting an idle
// session is LOSSLESS: returning to it rehydrates the full context on demand.
// Result: "many open chats" → "≤ MAX live processes + resume-on-demand".
const MAX_COWORK_SESSIONS = Math.max(1, parseInt(process.env.MAX_COWORK_SESSIONS || "4", 10));
// Hard ceiling for the busy-overflow case (all sessions running): we allow SOME
// overflow so a burst of concurrent tasks isn't rejected, but never unbounded — past
// this, creation is refused so the user's machine can't be flooded with agent
// processes. (G20)
const MAX_COWORK_HARD_CAP = Math.max(MAX_COWORK_SESSIONS,
                            parseInt(process.env.MAX_COWORK_HARD_CAP || String(MAX_COWORK_SESSIONS * 2), 10));
const IDLE_TIMEOUT_MS     = Math.max(60_000, parseInt(process.env.COWORK_IDLE_TIMEOUT_MIN || "15", 10) * 60_000);
const REAP_INTERVAL_MS    = 60_000;

class CoworkSessionManager {
  constructor(emit) {
    this.emit = emit;
    this.sessions = new Map();
    this._seq = 0;
    // Idle reaper: dispose sessions idle beyond the timeout that are NOT busy.
    this._reaper = setInterval(() => this._reapIdle(), REAP_INTERVAL_MS);
    if (this._reaper.unref) this._reaper.unref();  // never keep the process alive
  }

  // opts: { resumeId?, gatewayBase, jwt } — gatewayBase + jwt wire the connector MCP.
  async create(cwd, opts = {}) {
    this._evictIfFull();  // make room BEFORE spawning so we never exceed the soft cap
    // G20: if we're STILL at/above the hard cap (everything busy, no idle to evict),
    // refuse rather than spawn unbounded processes on the user's machine.
    if (this.sessions.size >= MAX_COWORK_HARD_CAP) {
      return { error: "too_many_sessions",
               message: `Too many tasks are running at once (${this.sessions.size}). `
                      + "Wait for one to finish or stop it, then try again." };
    }
    const id = `co${++this._seq}_${Date.now().toString(36)}`;
    const session = new CoworkOfficeSession(id, cwd, this.emit, opts || {});
    this.sessions.set(id, session);
    const ok = await session.start();
    return { id, ready: ok, cwd, resumeId: (opts && opts.resumeId) || null };
  }

  // Evict the least-recently-used NON-busy session when at capacity. A busy session
  // (a running task) is never evicted; if all are busy we allow a temporary overflow
  // rather than kill in-flight work — the reaper trims it once one goes idle.
  _evictIfFull() {
    while (this.sessions.size >= MAX_COWORK_SESSIONS) {
      let lru = null;
      for (const s of this.sessions.values()) {
        if (s.busy) continue;
        if (!lru || s.lastUsedAt < lru.lastUsedAt) lru = s;
      }
      if (!lru) break;  // everything is busy → allow overflow
      this.close(lru.id);
    }
  }

  _reapIdle() {
    const now = Date.now();
    for (const s of [...this.sessions.values()]) {
      if (!s.busy && (now - (s.lastUsedAt || 0)) > IDLE_TIMEOUT_MS) {
        this.close(s.id);
      }
    }
  }

  get(id) { const s = this.sessions.get(id); if (s) s.lastUsedAt = Date.now(); return s; }
  run(id, payload) { const s = this.get(id); if (s) s.run(payload); }
  respondConfirm(id, confirmId, answer) { const s = this.get(id); if (s) s.respondConfirm(confirmId, answer); }
  interrupt(id) { const s = this.get(id); if (s) s.interrupt(); }
  setModel(id, model) { const s = this.get(id); return s ? s.setModel(model) : Promise.resolve(false); }
  setPermissionMode(id, mode) { const s = this.get(id); return s ? s.setPermissionMode(mode) : Promise.resolve(false); }
  getContextUsage(id) { const s = this.get(id); return s ? s.getContextUsage() : Promise.resolve(null); }
  close(id) { const s = this.sessions.get(id); if (s) { s.dispose(); this.sessions.delete(id); } }
  disposeAll() {
    if (this._reaper) { clearInterval(this._reaper); this._reaper = null; }
    for (const s of this.sessions.values()) s.dispose();
    this.sessions.clear();
  }
}

module.exports = { CoworkSessionManager };
