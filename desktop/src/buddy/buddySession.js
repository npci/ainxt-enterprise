// SPDX-License-Identifier: MIT
"use strict";
// ─── BUDDY CLI TRACER ─────────────────────────────────────────────────────────
// Set AINXT_CLI_TRACE=1 in ainxt-desktop.bat to enable.
// Logs to: %USERPROFILE%\.ainxt\buddy-trace.log
// Tail:    Get-Content "$env:USERPROFILE\.ainxt\buddy-trace.log" -Wait
// ─────────────────────────────────────────────────────────────────────────────
const _traceEnabled = process.env.AINXT_CLI_TRACE === "1";
const _traceFile = require("path").join(require("os").homedir(), ".ainxt", "buddy-trace.log");
function _trace(tag, data) {
  if (!_traceEnabled) return;
  try {
    const line = `[${new Date().toISOString()}] [${tag}] ${typeof data === "string" ? data : JSON.stringify(data)}\n`;
    require("fs").appendFileSync(_traceFile, line);
  } catch { /* best-effort */ }
}

// `ainxt mcp add` exits non-zero when the server is already registered, printing
// "MCP server <name> already exists in user config". execFileSync surfaces the
// child's output on err.stdout / err.stderr (Buffers) and the summary on
// err.message. That case is idempotent success — the server IS present — so we
// must NOT treat it as a connector outage. Returns true when the failure is only
// "already exists".
function _mcpAlreadyExists(err) {
  if (!err) return false;
  const parts = [err.message, err.stdout, err.stderr]
    .map((v) => (v == null ? "" : v.toString()))
    .join("\n")
    .toLowerCase();
  return parts.includes("already exists");
}

// ─── Runaway-loop circuit breaker helpers ──────────────────────────────────────
// Tool names whose repeated identical calls are most often caused by silent
// upstream truncation (a large Excel/PDF/doc extraction got cut, the model
// concludes the read failed, and retries the exact same call). Flagging these
// specifically in the trip message lets support diagnose a "stuck in a loop"
// report as a truncation issue from the error text alone, without pulling a
// full trace file.
const _EXTRACTION_TOOL_NAMES = /extract_document|^read$|read_file|upload_file_to_chat/i;

function _isExtractionTool(name) {
  return _EXTRACTION_TOOL_NAMES.test(String(name || ""));
}

// Builds the user-facing "runaway loop" error message. When the repeated tool
// looks like a document-extraction call, add a pointed hint about truncation —
// this is overwhelmingly the actual root cause on large Excel files (see the
// EXTRACT_TABLE_ROW_LIMIT/EXTRACT_TEXT_LIMIT truncation-warning work in
// desktop/src/main.js) rather than a genuine infinite-loop bug.
function _loopTripMessage(name) {
  const base = `Stopped a runaway loop — the assistant kept making the same "${name}" call. ` +
    `Aborting this turn to protect your budget.`;
  if (_isExtractionTool(name)) {
    return `${base} This usually means the file was only partially read (e.g. a large ` +
      `spreadsheet whose row/character cap was hit) and the assistant kept re-trying to ` +
      `"get the rest" instead of reporting the data as partial. Try attaching a smaller ` +
      `slice of the file, or ask about a specific sheet/range.`;
  }
  return `${base} Try rephrasing the request.`;
}

// Trip thresholds — env-tunable, defaults unchanged (6 identical calls / 400
// total) to avoid a behavior regression for unrelated tool-use patterns. Once
// the desktop's own extraction truncation is fixed (see
// EXTRACT_TABLE_ROW_LIMIT/EXTRACT_TEXT_LIMIT in desktop/src/main.js), this guard
// should rarely fire on Excel files at all; if a deployment wants a tighter
// safety net once that's confirmed, AINXT_LOOP_SAME_COUNT can be lowered (e.g.
// to 3) without a code change.
const _LOOP_SAME_COUNT_THRESHOLD = Number(process.env.AINXT_LOOP_SAME_COUNT) || 6;
const _LOOP_TOTAL_CALLS_THRESHOLD = Number(process.env.AINXT_LOOP_TOTAL_CALLS) || 400;
// ─────────────────────────────────────────────────────────────────────────────

const TLS_INSECURE = process.env.AINXT_DESKTOP_TLS_INSECURE === "1";

// Default model when neither the caller nor the session specifies one.
// Was a hardcoded cloud id, so a deployment running its own models spawned
// the CLI with a model it could not route to.
const DEFAULT_CLI_MODEL = process.env.AINXT_DEFAULT_MODEL || "";
if (TLS_INSECURE) {
  console.warn("[ainxt-desktop] AINXT_DESKTOP_TLS_INSECURE=1 — TLS certificate " +
    "verification is disabled. Only use this on trusted internal networks.");
}
const TLS_ENV = TLS_INSECURE
  ? { AINXT_TLS_INSECURE: "1", AINXT_INSECURE_TLS: "1" }
  : {};

/**
 * BuddyOfficeSession — drives the FULL ainxt agent (app/main.tsx) headless over
 * the stream-json protocol, configured as an OFFICE assistant
 * (the "Buddy" tab) rather than the in-repo coding agent (the "Code" tab).
 *
 * This is the local-agent half of the P0 connector bridge: the agent runs on the
 * user's machine but reaches AiNxt connectors, the Knowledge Base, and documents
 * through the gateway's Buddy MCP server, exposed as an `sse` MCP server pointed
 * at <gatewayBase>/ainxt/v1/api/buddy/mcp/sse and authenticated with the user's
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
 * AiNxt guardrails (enforced server-side; honoured here by design):
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
const { resolveCliBinary, missingCliMessage } = require("./binary");
const { resolveProtocol } = require("./protocol");

// Connector tools that SEND to other people or IRREVERSIBLY change state. These
// must ALWAYS confirm (never auto-allowed by accept/bypass permission modes) — the
// MCP tool name is `mcp__ainxt_buddy__<tool>` or the bare connector tool name.
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

// Local tools that MODIFY state and must always prompt (in addition to the
// destructive connector sends above). Reads/searches/doc-generation are NOT here.
const _SENSITIVE_LOCAL_TOOLS = new Set([
  "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash",
]);
// A tool call is "sensitive" (must prompt) if it is a destructive/outbound
// connector tool OR a local state-changing tool. Everything else is auto-allowed
// so the user is only interrupted for real actions (sends, writes), matching the
// old CLI's confirm UX.
function _isSensitiveTool(toolName) {
  const n = String(toolName || "");
  if (_isDestructiveConnectorTool(n)) return true;
  const base = n.includes("__") ? n.slice(n.lastIndexOf("__") + 2) : n;
  return _SENSITIVE_LOCAL_TOOLS.has(base) || _SENSITIVE_LOCAL_TOOLS.has(n);
}

function toolDetail(input) {
  if (!input || typeof input !== "object") return "";
  const v = input.command || input.file_path || input.path || input.pattern || input.url || "";
  return v ? String(v).slice(0, 80) : "";
}

// The ACP CLI wraps EVERY real tool call behind a generic meta-tool — `use_tool`
// (actually calling an MCP tool) or `search_tool` (looking one up) — so the UI
// otherwise only ever sees "use_tool"/"search_tool" chips, never the tool that's
// actually running (e.g. `ainxt_buddy__people_search`). The real target is
// carried inside rawInput: `use_tool` puts it in `tool_name`; `search_tool` puts
// the query directly in `query`. Unwrap it so the tool-call UI (and the loop
// guard / destructive-tool / onedrive checks below, which key off the NAME) see
// the real tool, not the wrapper.
function _realToolName(name, rawInput) {
  if (rawInput && typeof rawInput === "object") {
    if (rawInput.tool_name) return rawInput.tool_name;                 // use_tool
    if (name === "search_tool" && rawInput.query) return `search_tool: "${rawInput.query}"`;
  }
  return name;
}

// ── Connector tool name helpers (for platform-wide permissions table) ─────────
// Mirrors mcp_bridge.py _KNOWN_CONNECTOR_PREFIXES. Used to split an MCP-qualified
// tool name (e.g. "ainxt_buddy__microsoft_365_outlook_send_mail") into the
// {connector, tool} pair stored in ainxt.user_connector_permissions.
// Order matters: longer/more-specific prefixes must come before shorter ones
// (e.g. "jira_connector_" before "jira_") so the right connector is matched.
const _CONNECTOR_PREFIXES = [
  "microsoft_365_", "gmail_", "jira_connector_", "slack_", "github_",
  "confluence_", "google_drive_", "onedrive_", "sharepoint_",
  "gitlab_", "jira_",
];

/**
 * Parse an MCP-qualified tool name into { connector, tool } for the permissions
 * table. Strips the MCP server prefix ("mcp__ainxt_buddy__" or "ainxt_buddy__")
 * then matches the first known connector prefix.
 *
 * Returns null for local tools (Write, Edit, Bash, …) that are not connector calls.
 *
 * Examples:
 *   "mcp__ainxt_buddy__microsoft_365_outlook_send_mail"
 *     → { connector: "microsoft_365", tool: "outlook_send_mail" }
 *   "ainxt_buddy__gitlab_list_projects"
 *     → { connector: "gitlab", tool: "list_projects" }
 *   "Write" → null
 */
function _parseConnectorTool(toolName) {
  let name = String(toolName || "");
  // Strip MCP server prefix — both old (mcp__ainxt_buddy__) and new (ainxt_buddy__) CLIs
  if (name.startsWith("mcp__ainxt_buddy__")) name = name.slice("mcp__ainxt_buddy__".length);
  else if (name.startsWith("ainxt_buddy__")) name = name.slice("ainxt_buddy__".length);
  else return null; // local tool (Write, Edit, Bash, etc.) — not a connector call

  for (const prefix of _CONNECTOR_PREFIXES) {
    if (name.startsWith(prefix)) {
      const connector = prefix.slice(0, -1); // strip trailing "_"
      const tool = name.slice(prefix.length);
      if (!tool) return null; // malformed — no tool name after prefix
      return { connector, tool };
    }
  }
  return null; // unknown connector prefix (e.g. a non-connector MCP tool)
}

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

  return { path, name: _basename(path), added, removed, isNew, lines, truncated: 0 };
}

// Office system prompt — the Buddy counterpart of cliManager's REPO_PROMPT.
// Tells the agent it is an office assistant wired to the deployer's own
// connectors / KB / docs via its MCP tools, so it must NEVER ask the user to
// paste data, and that any outbound write (email / message / doc) is
// confirmed + compliance-gated. ORG_NAME is deployer-configurable so this
// prompt isn't hardcoded to any one organization.
const ORG_NAME = process.env.ORG_NAME || "your organization";
const OFFICE_PROMPT =
  `You are AiNxt Buddy — an AI OFFICE ASSISTANT for a ${ORG_NAME} employee (a NON-engineer). ` +
  "You do knowledge work: read and summarize emails/messages/documents, draft replies and " +
  "updates, prepare reports, and pull information from the user's connected apps — using ONLY " +
  "the office tools provided to you via your MCP servers (connectors, documents, calculations, " +
  "and — only when explicitly listed in your tools — browser & computer control).\n" +
  "YOU ARE NOT A DEVELOPER. You must NEVER run OS commands, use a shell, curl/HTTP an API, edit " +
  "code, or inspect the user's system. A `Bash`/shell tool may APPEAR in your tool list — ignore it " +
  "completely; it is not yours to use and it CANNOT reach the user's work systems (those are remote " +
  "SaaS apps, not files on this machine). If a task seems to need a shell, the right answer is almost " +
  "always a connector tool — or tell the user it's not available.\n" +
  "CONNECTORS FIRST — THIS IS YOUR PRIMARY INSTINCT. The user's work lives in remote systems " +
  "(GitLab, Jira, Outlook, Teams, Confluence, SharePoint), and you reach ALL of them through your " +
  "connector tools. Before you answer ANY question about the user's work — code, repos, tickets, " +
  "mail, meetings, documents — your FIRST move is to look at your connector tool list and call the " +
  "tool that fits. The user will usually NOT name the system: they say \"my open MRs\", not \"use the " +
  "GitLab connector\". That is still a connector question. NEVER ask which system they mean when the " +
  "vocabulary already tells you, and NEVER answer such a question from your own knowledge, from the " +
  "local filesystem, or with a shell command.\n" +
  "WORK-SYSTEM VOCABULARY → TOOL (call these DIRECTLY via use_tool; do NOT use search_tool first):\n" +
  "• \"merge request\", \"MR\", \"PR\", \"pull request\", \"waiting on my review\", \"ready to merge\" → " +
  "  `gitlab_list_my_mrs` for the user's own MRs across all projects (NO project needed), or " +
  "  `gitlab_list_mrs` when they named a project. Use `gitlab_get_mr_files` for what changed.\n" +
  "• \"repo\", \"repository\", \"project\", \"which repos do I have\" → `gitlab_list_projects`. " +
  "  \"commits\", \"latest changes\", \"who changed X\" → `gitlab_list_commits`. To read a file from a " +
  "  repo use `gitlab_read_file` — NOT Read (repo files are remote, not on this machine).\n" +
  "• An issue key like ABC-123 / PAY-4521 → `jira_get_issue` with that key. Do this even when the " +
  "  user just pastes the bare key with no other words.\n" +
  "• \"ticket\", \"bug\", \"story\", \"sprint\", \"backlog\", \"assigned to me\", \"my open issues\" → " +
  "  `jira_search_issues` with JQL. For the user's own work use `assignee = currentUser()` " +
  "  (e.g. \"assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC\"). " +
  "  For GitLab issues use `gitlab_list_my_issues` (all projects) or `gitlab_list_issues`.\n" +
  "CRITICAL — NEVER use Bash, `git`, `curl`, Grep, Glob, or the local filesystem to answer a GitLab " +
  "or Jira question. GitLab and Jira are remote servers reachable ONLY through the connector tools " +
  "above; there is no local clone and no shell that can see them. Running `git log` or grepping a " +
  "folder is ALWAYS the wrong answer to \"show me my merge requests\".\n" +
  "IF A NEEDED CONNECTOR IS MISSING from your tool list, say so plainly and tell the user to connect " +
  "it in Profile → API Token Vault (GitLab needs a Personal Access Token; Jira needs " +
  "`email:api_token`). Do NOT silently fall back to a shell, a web search, or a guess — and do NOT " +
  "claim the system is unavailable if the tool IS in your list.\n" +
  "IF THE USER DIDN'T NAME A PROJECT and the tool you need requires one, call `gitlab_list_projects` " +
  "(or the cross-project `gitlab_list_my_mrs` / `gitlab_list_my_issues`) to discover it. Never invent " +
  "a project path, and never abandon the connector just because one parameter is missing.\n" +
  "FILE ACCESS — be accurate about this: you can ONLY work with files INSIDE the user's attached folder. " +
  "To see what files are in it, call the `list_files` tool (it lists exactly the attached folder). The " +
  "files are also listed below under '## Files in your attached folder'. For Word/Excel/PDF/CSV/text files, " +
  "call `extract_document` on the file path from list_files; it extracts readable text, tables, sheets/pages, " +
  "and amount evidence from .docx, .xlsx, .xlsm, .pdf, .csv, .txt, and .md files (legacy .xls returns an " +
  "unsupported-format notice — ask the user to re-save as .xlsx). Use Read only for " +
  "simple plain-text files if extract_document is not needed. You CANNOT read any path outside that folder " +
  "(/etc, the home directory, other folders) — such reads are blocked. NEVER claim you can read 'any file on " +
  "the machine if they give a path' (false), and never say the folder is empty if files are listed. If no " +
  "folder is attached, you have NO local file access — call list_files only after one is attached; otherwise " +
  "use connectors.\n" +
  "SENDING FILES AS ATTACHMENTS — follow these rules exactly:\n" +
  "CRITICAL: Do NOT use search_tool to look up outlook_send_mail, teams_send_chat_message, or " +
  "teams_send_message — call them directly via use_tool. Their schemas are KNOWN and both tools " +
  "accept attachment_ids. NEVER say these tools do not support attachment_ids — they do.\n" +
  "CRITICAL: `outlook_send_mail` `to` (and cc/bcc) must be a PLAIN STRING (e.g. \"a@x.com\" or " +
  "\"a@x.com, b@x.com\"), NEVER a JSON array. Passing an array is rejected.\n" +
  "CRITICAL — MESSAGE FORMATTING: write the Teams `message` and the Outlook `body` as PLAIN TEXT " +
  "using real newlines for line breaks. Do NOT write HTML: no <br>, no <p>, no &amp; entities — " +
  "they are shown literally to the recipient. For an HTML email, set html=true on " +
  "outlook_send_mail and only then use HTML in `body`. `teams_send_chat_message` has NO html " +
  "parameter — always send plain text there.\n" +
  "CRITICAL: when the message already carries attachment_ids for a folder file, ATTACH THAT FILE AS-IS. " +
  "Do NOT rebuild/recreate it with build_document, do NOT convert it to a DOCJOB — send the exact " +
  "uploaded file via its attachment_ids.\n" +
  "• If the message contains a folder auto-upload note (a name -> attachment_id map for folder files), " +
  "  those ids ARE the corresponding files — do NOT call upload_file_to_chat again for them. But the map " +
  "  lists what is AVAILABLE, not a command to attach everything in it: pass ONLY the attachment_ids for " +
  "  the exact files you tell/confirm with the user. If the user asked for a specific type or a subset " +
  "  (e.g. \"the pptx and txt files\", \"just the report\") and the map also contains other files (e.g. " +
  "  from a broader match or a different type), you MUST filter the map down to the id(s) whose file name " +
  "  matches what you said you'd send — the number of attachment_ids in your send call MUST equal the " +
  "  number of files you named/confirmed to the user, never the full map by default.\n" +
  "• If the user UPLOADED a file INTO THIS CHAT (via the 📎 paperclip button), the message contains " +
  "  [attachment_id=XXXX]. RULE: you MUST include `attachment_ids=[\"XXXX\"]` in EVERY call to " +
  "  `outlook_send_mail`, `teams_send_chat_message`, and `teams_send_message` — no exceptions, no matter " +
  "  what. If you have 3 attachment_ids, pass all 3: `attachment_ids=[\"id1\",\"id2\",\"id3\"]`. " +
  "  NEVER call any send tool without attachment_ids when files are present in the conversation. " +
  "  NEVER use `attachment_file_path`. The attachment_id IS the file — do not re-upload or re-read it. " +
  "  RETRY RULE: if a send call fails (e.g. wrong recipient format) and you retry with a corrected " +
  "  chat_id or email, you MUST carry the same attachment_ids into the retry call — never drop them.\n" +
  "• If the file is IN THE ATTACHED FOLDER and the user wants to send it as an attachment: call " +
  "  `upload_file_to_chat` with the exact filename from `list_files` — this is a LOCAL DESKTOP TOOL, " +
  "  call it directly via use_tool (do NOT use search_tool to look it up first). It returns an " +
  "  `attachment_id` you MUST then pass to the send tool. For multiple files, call `upload_file_to_chat` " +
  "  once per file, collect ALL returned attachment_ids, then pass them ALL in one send call. " +
  "  CRITICAL — do NOT use onedrive_upload for this (onedrive_upload is ONLY for uploading content to " +
  "  the user's OneDrive cloud storage, NOT for attaching local files to emails or Teams messages). " +
  "  Do NOT base64-encode files. Do NOT use attachment_file_path. Do NOT ask the user to use the " +
  "  paperclip button for files that are already in the attached folder — upload them yourself.\n" +
  "• Use `attachment_job_id`/`attachment_artifact_id` ONLY for AiNxt-generated DOCJOB/artifact files " +
  "  (files built by `build_document`).\n" +
  "• If no upload id was provided and the file is not in the attached folder, ask the user to upload it " +
  "  via the 📎 button first — do NOT attempt to send without a valid attachment_id.\n" +
  "FOR CALCULATIONS / DATA WORK you DO have one tool: `run_code`. It runs a short script in a SECURE, " +
  "network-isolated, throwaway SANDBOX (not the user's machine, no internet, destroyed after each run) " +
  "and returns only what the script prints. First extract local files with `extract_document`, then put the " +
  "extracted text/tables into run_code or analyze_data for accurate totals, parsing/reshaping, reconciliation, " +
  "or date arithmetic. This is for computing answers, never for operating the computer. Write for a " +
  "non-technical audience: present results, not code.\n" +
  "DOCUMENTS (Word .docx, PowerPoint .pptx, Excel .xlsx, PDF) — first use `extract_document` when the task " +
  "depends on attached source files. For PDF-to-Excel reports, extract every PDF, preserve source filename/page " +
  "evidence for amounts, reconcile totals, and generate the workbook directly; do NOT ask for final confirmation " +
  "unless the user explicitly requests approval/review. Produce professional, EDITABLE files using " +
  "the document SKILLS — call these MCP tools DIRECTLY via use_tool, do NOT use search_tool to look them up " +
  "first (their names are known): (1) call `get_document_skill` with the format to read the exact rules and " +
  "code patterns; (2) write the COMPLETE build code it tells you to (ainxt-doc JS for docx, ainxt-deck JS for " +
  "pptx, ainxt_sheet Python for xlsx, ainxt-doc JS for pdf) following the skill's styling guidance — write the " +
  "FULL script in one go, do NOT stop and wait for user confirmation mid-way; (3) call `build_document` with " +
  "that complete code. CRITICAL: NEVER call build_document with empty or partial `code`. NEVER pause between " +
  "steps to ask the user — execute all three steps autonomously in a single turn. " +
  "It runs in the secure sandbox and returns a [DOCJOB:...] marker — include it VERBATIM so " +
  "the user gets a rendered preview + download. If the build reports an error, fix the code and call again. " +
  "To revise later, call build_document again with updated code. Use `generate_document` only for plain " +
  "Markdown. Always show the [DOCJOB:...] marker; never paste raw code to the user.\n" +
  "RESEARCH / ANALYSIS / 'brief me on' / 'compare' / 'write a report on' tasks — use the `deep_research` " +
  "tool. It runs AiNxt's multi-model engine, which cross-examines several models and is more rigorous than " +
  "answering directly. FIRST gather real material with your connector/file tools (relevant emails, Teams " +
  "messages, SharePoint/Drive docs, attached files), then pass each item in `sources` so the report carries " +
  "real [n] citations; set `depth` to 'deep' for thorough asks. Relay the returned report to the user, and " +
  "offer to turn it into a Word/PDF/PPT file via the document skills.\n" +
  "NEVER ask the user to paste, forward, upload, or copy in emails, messages, tickets, or documents " +
  "— call the appropriate connector/document/browser tool to fetch them yourself, then act. Reading " +
  "is safe (the platform redacts sensitive data on the way back). For Teams, Outlook, and Calendar " +
  "recipients: if the user gives a name or partial email, call `people_search` first. Use exact " +
  "confirmed email/UPN matches directly; if multiple people match, show the candidates and ask the " +
  "user which exact person to use before calling any send/invite/chat tool. NEVER silently pick the " +
  "first person. Any OUTBOUND action — sending an email/message, posting to a connector, creating a " +
  "document — goes through your action tools, which are compliance-gated and require explicit user " +
  "confirmation: propose it and let the confirm flow run. " +
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
  "- OFFICE: connectors (Outlook/Teams/GitLab/Jira/Confluence), document generation " +
  "(get_document_skill → build_document), `run_code` sandbox, `deep_research`, and " +
  "`retrieve` (Knowledge Base).\n" +
  "You DO have a terminal and full filesystem access — never say you don't. When a task " +
  "needs shell/files/web, just do it with the tools above.\n" +
  "CONNECTORS FIRST FOR REMOTE SYSTEMS — this is a HARD RULE, not a preference. Your local " +
  "tools can only see THIS machine. Anything that lives on a remote server — GitLab, Jira, " +
  "Outlook, Teams, Confluence — must go through its connector tool, even though you also " +
  "have a shell. The user will not name the system: \"my open MRs\" is a GitLab connector " +
  "question, \"what's the status of ABC-123\" is a Jira connector question. Check your " +
  "connector tool list FIRST for any question about the user's work.\n" +
  "• \"MR\", \"merge request\", \"PR\", \"needs my review\" → `gitlab_list_my_mrs` (all projects) " +
  "  or `gitlab_list_mrs` (named project) — NEVER `git log`, `git branch`, or `curl`.\n" +
  "• \"repos\"/\"projects\" → `gitlab_list_projects`; repo file contents → `gitlab_read_file`.\n" +
  "• A key like ABC-123 / PAY-4521 → `jira_get_issue`. \"tickets\"/\"assigned to me\"/\"sprint\" → " +
  "  `jira_search_issues` with JQL (`assignee = currentUser()` for the user's own work).\n" +
  "• Send mail via the Outlook tool, not by scripting.\n" +
  "Use Bash/Read/Grep for LOCAL work — this repo, this filesystem, running programs. A local " +
  "git checkout is NOT a substitute for GitLab: `git log` shows one clone's history, not the " +
  "user's merge requests, review queue, or projects across the server.\n" +
  "OUTBOUND connector actions (send email, post to Teams, create a doc) are still " +
  "compliance-gated. For everything else, act directly and stream your progress.\n" +
  "DOCUMENTS: for polished Word/PPT/Excel/PDF deliverables prefer the document skills " +
  "(get_document_skill → build_document) so the user gets a previewable, branded file.\n" +
  "Sub-agents (Task) are available for parallel/delegated work. " +
  "MEMORY — you have a `remember` tool; proactively save durable facts about the user " +
  "(preferences, role, recurring people/projects) and treat facts in your operating " +
  "context as things you already KNOW.";

// Buddy MCP path on the gateway (the connector/KB/docs bridge, SSE transport).
const BUDDY_MCP_PATH = "/ainxt/v1/api/buddy/mcp/sse";

class BuddyOfficeSession {
  constructor(id, cwd, emit, opts = {}) {
    this.id = id;
    this.cwd = cwd;
    this.emit = emit;
    this.resumeId = opts.resumeId || null;   // resume an existing on-disk session
    this.sessionId = opts.resumeId || null;   // real agent session_id (from init)
    // Durable conversation id (from the UI's convId). Injected into config.toml
    // [models].extra_headers as x-ainxt-conv-id at spawn time so BOTH the old
    // (streamjson --full, persistent) and new (ACP, persistent) CLIs pick it up
    // before their first inference request. Updated per-turn by run() for the
    // case where convId wasn't known at create time (brand-new conversation).
    this._convId = opts.convId || null;
    // Currently selected model. Optional at construction (create() may pass one so
    // a fresh session starts on the previously-picked model instead of always
    // defaulting to Sonnet 4.6); setModel() updates it for subsequent turns/spawns.
    // Falls back to "claude-sonnet-4-6" everywhere this is read, so an omitted/empty
    // value reproduces the EXACT previous hardcoded behaviour.
    this._currentModel = opts.model || null;
    // A model switch requested WHILE a turn is in flight (ACP only) is deferred
    // here instead of tearing down the process mid-turn; drained by the
    // turn_complete handler once the turn finishes. Always null on streamjson
    // (no persistent process to defer against).
    this._pendingModelSwitch = null;
    // True only for the duration of an intentional respawn-for-model-change
    // teardown, so the shared `proc.on("close", ...)` handler can tell "the
    // user switched models" apart from a real crash/exit and skip emitting
    // session:exit / cleaning up MCP+scratch state for what is actually just
    // an internal restart.
    this._respawning = false;
    this.computerUse = !!opts.computerUse;    // browser + native control exposed this session
    // Full local-agent power: shell, file read/write/edit, code search, web — the same
    // tools the Code tab has. When true, Buddy is unrestricted (no tool stripping, no
    // folder jail, no per-action confirm). Gated by a deployment flag (default off) so a
    // deployment can keep the office-only posture. See _spawn / _systemPrompt.
    this.devTools = !!opts.devTools;
    // Which CLI wire protocol this session drives (see ./protocol.js):
    //   "streamjson" → newline-delimited JSON over --print   [default]
    //   "acp"        → Agent-Client-Protocol over agent stdio  [opt-in]
    // Resolved ONCE per session so a mid-session flag flip can't split a session's
    // protocol between spawn and stdout parsing.
    this._protocol = resolveProtocol();
    this._permMode = "default";               // default | acceptEdits | plan | bypassPermissions
    // Tools the user chose "Always allow" for during THIS session. This is the
    // ONLY way a tool call skips the Allow/Don't Allow prompt — an explicit user
    // decision, never a silent default. Cleared when the session is disposed.
    this._alwaysAllowTools = new Set();
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
    this._pendingPerms = new Map(); // request_id → {tool, input}
    this._pendingCtrl = new Map();  // request_id → {resolve, reject}
    this._lastTotalCost = 0;
    // Running sessionCostTicks from the CLI's ACP streaming chunks (ACP path only).
    // The CLI computes cost internally using its own rate table and sends the
    // cumulative session cost as ticks on every agent_message_chunk _meta.
    // Divide by 1e11 to get USD — same value the CLI TUI displays.
    // Reset to 0 after each turn result is emitted.
    this._lastSessionCostTicks = 0;
    // Set to true by the turn_completed session/update handler so the
    // session/prompt result handler knows not to emit a duplicate result.
    // Document/tool-only turns fire turn_completed first, then the result
    // arrives with zero tokens — without this guard a ghost $0.00/0tok
    // message appears after every document build.
    this._turnCompletedEmitted = false;
    this._deltasSinceAssistant = false;
    this._streamBuffer = "";        // accumulates agent_message_chunk text for final result
    this._currentTurnId = null;     // id of the in-flight session/prompt RPC
    this._mcpConfigPath = null;     // kept for cleanup compat (not used in ACP mode)
    this._injectedMcpServers = [];  // MCP server names written to config.toml (cleaned up on close)
    // True when connector MCP registration failed → the agent has no connector tools
    // this session. Surfaced to the user AND injected into the system prompt so the
    // agent reports the outage instead of improvising with its built-in shell.
    this._connectorsUnavailable = false;
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
      if (allow.length) headers["x-buddy-allowed-tools"] = allow.join(",");
      mcpServers.ainxt_buddy = { type: "sse", url: `${base}${BUDDY_MCP_PATH}`, headers };
    }
    // Desktop local MCP (browser automation + local files) — gives Buddy its
    // "computer use" (web) via Playwright, with per-action confirms enforced in
    // playwrightManager. Loopback only.
    if (this.localMcpPort) {
      // surface=buddy → the local MCP serves ONLY office tools (browser +
      // computer-use + folder-scoped list_files/extract_document). NO shell, no broad FS.
      // `root` = the granted folder; file tools refuse anything outside it.
      const root = this._grantedRoot ? `&root=${encodeURIComponent(this._grantedRoot)}` : "";
      mcpServers.ainxt_desktop = {
        type: "sse",
        url: `http://127.0.0.1:${this.localMcpPort}/sse?surface=buddy${root}`,
      };
    }
    if (Object.keys(mcpServers).length === 0) return null;
    const cfg = { mcpServers };
    try {
      const file = path.join(
        os.tmpdir(),
        `ainxt-buddy-mcp-${this.id}-${randomUUID()}.json`
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
  // office base, or just the office base. Role prompts come from buddy_roles.
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
    // Connector registration failed for this session — the agent has NO connector
    // tools. Tell it explicitly so it reports the outage instead of improvising with
    // its built-in Bash/Read (the "went to the command line" failure mode).
    if (this._connectorsUnavailable) {
      prompt += "\n\n## Connectors are UNAVAILABLE this session\n" +
        "Your connector tools failed to load, so you CANNOT reach GitLab, Jira, Outlook, Teams, " +
        "Confluence or SharePoint right now. If the user asks for anything from those systems, say " +
        "plainly that the connection failed and ask them to restart Buddy. Do NOT try to work around " +
        "it with a shell command, `git`, `curl`, the local filesystem, or a guess from memory, and do " +
        "NOT invent merge requests, tickets, emails or meetings.";
    }
    if (rp) prompt += `\n\n[ROLE — ${this.role && this.role.name ? this.role.name : "Specialist"}]\n${rp}`;
    // Persistent PROJECT context — instructions + accumulated memory that carry
    // across every task in the project (project scope + memory).
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
          `path itself (that fails — Read opens a FILE, not a directory). To work with Office/PDF/CSV files, call ` +
          `extract_document on the listed path; use Read only for plain text. The files:\n` +
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
        const u = new URL(`${base}/ainxt/v1/api/buddy/memory/prompt`);
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
    // Pre-populate _alwaysAllowTools from the gateway DB so connector tools the
    // user previously always-allowed are auto-approved without prompting again.
    await this._loadPersistedPermissions().catch(() => {});
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
        const u = new URL(`${base}/ainxt/v1/api/buddy/roles/${encodeURIComponent(roleId)}/context`);
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

  // ── Platform-wide always-allow permissions ────────────────────────────────

  /**
   * Persist an "always allow" decision for a connector tool to the gateway DB
   * (ainxt.user_connector_permissions). Fire-and-forget — never throws or blocks.
   *
   * Only connector tools are persisted (local tools like Write/Edit return null
   * from _parseConnectorTool and are silently skipped).
   */
  _persistAlwaysAllow(toolName) {
    const parsed = _parseConnectorTool(toolName);
    if (!parsed || !this.gatewayBase || !this.jwt) return; // local tool or no gateway
    try {
      const base = String(this.gatewayBase).replace(/\/+$/, "");
      const u = new URL(`${base}/ainxt/v1/api/connectors/permissions`);
      const lib = u.protocol === "https:" ? require("https") : require("http");
      const body = JSON.stringify({ connector: parsed.connector, tool: parsed.tool, always_allow: true });
      const req = lib.request({
        hostname: u.hostname, port: u.port,
        path: u.pathname + u.search,
        method: "POST",
        headers: {
          "Authorization": `Bearer ${this.jwt}`,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      }, (res) => {
        // Best-effort: trace on failure but never throw (fire-and-forget)
        if (res.statusCode && res.statusCode >= 400) {
          _trace("PERSIST_ALWAYS_ALLOW_FAIL", { tool: toolName, connector: parsed.connector, status: res.statusCode });
        } else {
          _trace("PERSIST_ALWAYS_ALLOW_OK", { tool: toolName, connector: parsed.connector, dbTool: parsed.tool });
        }
        // Drain the response body so the socket is released
        res.resume();
      });
      req.on("error", (e) => _trace("PERSIST_ALWAYS_ALLOW_ERROR", { tool: toolName, err: e.message }));
      req.setTimeout(5000, () => { try { req.destroy(); } catch { /* ignore */ } });
      req.write(body);
      req.end();
    } catch (e) {
      _trace("PERSIST_ALWAYS_ALLOW_EXCEPTION", { tool: toolName, err: String(e) });
    }
  }

  /**
   * Load the user's persisted always-allow decisions from the gateway DB and
   * pre-populate _alwaysAllowTools so previously-approved tools are auto-allowed
   * immediately without prompting. Called once at session start (best-effort).
   *
   * Wildcard "*" rows are skipped — we can't enumerate all tools here; the
   * orchestrator DB gate handles them on the server-side path.
   */
  async _loadPersistedPermissions() {
    if (!this.gatewayBase || !this.jwt) return;
    try {
      const base = String(this.gatewayBase).replace(/\/+$/, "");
      const u = new URL(`${base}/ainxt/v1/api/connectors/permissions`);
      const lib = u.protocol === "https:" ? require("https") : require("http");
      const data = await new Promise((resolve) => {
        const req = lib.request({
          hostname: u.hostname, port: u.port,
          path: u.pathname + u.search,
          method: "GET",
          headers: { "Authorization": `Bearer ${this.jwt}` },
        }, (res) => {
          let raw = "";
          res.on("data", (d) => { raw += d; });
          res.on("end", () => { try { resolve(JSON.parse(raw)); } catch { resolve([]); } });
        });
        req.on("error", () => resolve([]));
        req.setTimeout(4000, () => { try { req.destroy(); } catch { /* ignore */ } resolve([]); });
        req.end();
      });
      if (!Array.isArray(data)) return;
      let loaded = 0;
      for (const row of data) {
        if (!row.always_allow) continue;
        const connector = row.connector_name;
        const tool = row.tool_name;
        if (!connector || !tool || tool === "*") continue; // skip wildcards
        // Reconstruct both MCP-qualified name forms the CLI may use:
        //   streamjson (old CLI): "mcp__ainxt_buddy__<connector>_<tool>"
        //   ACP (new CLI):        "ainxt_buddy__<connector>_<tool>"
        // list_tools() in mcp_bridge.py exposes connector tools with a single "_"
        // between connector and tool (e.g. microsoft_365_outlook_send_mail), so
        // we mirror that here.
        const bare = `${connector}_${tool}`;
        this._alwaysAllowTools.add(`mcp__ainxt_buddy__${bare}`);
        this._alwaysAllowTools.add(`ainxt_buddy__${bare}`);
        loaded++;
      }
      _trace("LOADED_PERSISTED_PERMISSIONS", { loaded, total: data.length });
    } catch (e) {
      _trace("LOAD_PERSISTED_PERMISSIONS_ERROR", { err: String(e) });
    }
  }

  // ── Spawn args: ACP protocol (`agent stdio`) ───────────────────────────────
  _spawnArgsAcp(cwd) {
    const bin = resolveCliBinary();
    // Every other resolveCliBinary() site guards; this one built the arg list
    // straight from bin.args and threw a bare TypeError when the CLI was absent.
    if (!bin) throw new Error(missingCliMessage());
    return [
      ...bin.args,
      "--cwd", cwd,
      // Model is spawn-time for ACP (persistent process) — whatever was selected
      // via create({model}) or setModel() BEFORE this spawn. setModel() triggers a
      // respawn (see setModel()) when the model changes on an already-running ACP
      // session, so this always reflects the current selection.
      ...((this._currentModel || DEFAULT_CLI_MODEL) ? ["--model", this._currentModel || DEFAULT_CLI_MODEL] : []),
      // permission-mode "default": the CLI MUST ask before executing tools by
      // sending session/request_permission — which our _handleSdkMessage routes
      // to the Allow / Don't Allow / Always allow dialog. Using
      // "bypassPermissions" here made the CLI silently auto-execute every tool
      // (it never emitted a permission request), so connector SENDS ran without
      // the user's approval. "default" restores the confirm gate to match the
      // old CLI. (Safe/read tools are auto-allowed in the handler; only
      // writes/sends actually prompt.)
      "--permission-mode", "default",
      // Increase max turns so multi-step flows (get_document_skill → write code →
      // build_document, or upload N files → send) complete in a single turn.
      "--max-turns", "30",
      // Resume the same on-disk agent session across a respawn (e.g. a model
      // switch — see setModel()) so conversation history/context survives the
      // process teardown. this.sessionId is set once session/new responds
      // (_initializeAcp), so a respawn after the FIRST spawn always has it;
      // this.resumeId covers reopening a conversation from a previous app run.
      ...((this.sessionId || this.resumeId) ? ["--resume", this.sessionId || this.resumeId] : []),
      "agent", "stdio",
    ];
    // NOTE (ACP): --mcp-config/--disallowedTools/--allowedTools/--add-dir/
    // --append-system-prompt are old flags not supported here. MCP servers are
    // registered via `ainxt mcp add` (config.toml) in _injectMcpIntoConfig(); the
    // system prompt is injected into the first user message by run().
  }

  // ── OLD CLI (v1.0.2-beta) MCP registration via `ainxt mcp add` (SSE) ───────
  // The old CLI persists MCP servers to its own config, so each single-shot
  // `--json` run picks them up. Same command surface as the ACP path, but the
  // gateway connector uses --transport sse (the old CLI's default MCP transport).
  _injectMcpIntoConfigLegacy() {
    if (!this.gatewayBase || !this.jwt) return;
    const { execFileSync } = require("child_process");
    const bin = resolveCliBinary();
    if (!bin) return;
    const base = String(this.gatewayBase).replace(/\/+$/, "");
    const added = [];
    try {
      const mcpUrl = `${base}${BUDDY_MCP_PATH}`;
      const buddyArgs = [
        "mcp", "add",
        "--transport", "sse",
        "--header", `Authorization: Bearer ${this.jwt}`,
        "--scope", "user",
        "ainxt_buddy",
        mcpUrl,
      ];
      const allow = this.role && Array.isArray(this.role.allowed_connectors) ? this.role.allowed_connectors : [];
      if (allow.length) buddyArgs.push("--header", `x-buddy-allowed-tools: ${allow.join(",")}`);
      const insecureEnv = { ...process.env, ...TLS_ENV };
      // ALWAYS remove any existing entry first, then add fresh. The CLI persists
      // MCP servers (incl. the Authorization: Bearer <token> header) to its own
      // config.toml. On re-login the app writes a NEW token to config.json, but
      // the stored MCP header still carries the OLD token — and `mcp add` refuses
      // to overwrite an existing server ("already exists"). Result: every MCP
      // tool call authenticates with the STALE token → "Token expired or invalid"
      // → connectors appear unavailable even though login succeeded. Removing
      // first guarantees the add re-writes the current token on every session.
      try {
        execFileSync(bin.command, [...bin.args, "mcp", "remove", "--scope", "user", "ainxt_buddy"], {
          env: insecureEnv, timeout: 5000, stdio: "pipe",
        });
        _trace("MCP_INJECT", "[legacy] removed stale ainxt_buddy before re-add (token refresh)");
      } catch (_rmErr) { /* not present yet — fine, the add below creates it */ }
      execFileSync(bin.command, [...bin.args, ...buddyArgs], {
        // Set BOTH TLS-bypass names: the OLD prod CLI (v1.0.4) reads
        // AINXT_INSECURE_TLS; the NEW (ACP) CLI reads AINXT_TLS_INSECURE. The
        // MCP SSE client uses this to accept a self-signed gateway cert —
        // without the right name the connection fails and connector tools go dark.
        env: insecureEnv, timeout: 5000, stdio: "pipe",
      });
      added.push("ainxt_buddy");
      _trace("MCP_INJECT", `[legacy] Registered ainxt_buddy via mcp add --transport sse (${mcpUrl}) with fresh token`);
    } catch (err) {
      // `ainxt mcp add` exits non-zero when the server is ALREADY registered
      // ("... already exists in user config"). That is a SUCCESS for us — the
      // server is present — not an outage. Only a real add failure means the
      // connectors are unavailable, so don't scare the user (or tell the agent
      // connectors are down, which makes it refuse to use working tools).
      if (_mcpAlreadyExists(err)) {
        added.push("ainxt_buddy");
        _trace("MCP_INJECT", "[legacy] ainxt_buddy already registered — reusing existing entry");
      } else {
        _trace("MCP_INJECT_ERROR", `[legacy] ainxt_buddy: ${err.message}`);
        this._connectorsUnavailable = true;
        this.emit(this.id, {
          type: "notice", level: "warn",
          msg: "Couldn't reach your connectors this session — GitLab, Jira, Outlook and "
             + "Teams are unavailable. Restart Buddy; if it persists, sign out and back in.",
        });
      }
    }
    try {
      if (this.localMcpPort) {
        const root = this._grantedRoot ? `&root=${encodeURIComponent(this._grantedRoot)}` : "";
        execFileSync(bin.command, [
          ...bin.args, "mcp", "add", "--transport", "sse", "--scope", "user",
          "ainxt_desktop", `http://127.0.0.1:${this.localMcpPort}/sse?surface=buddy${root}`,
        ], { env: { ...process.env }, timeout: 5000, stdio: "pipe" });
        added.push("ainxt_desktop");
        _trace("MCP_INJECT", "[legacy] Registered ainxt_desktop via mcp add --transport sse");
      }
    } catch (err) {
      // Same as above: "already exists" is success, not a failure. The desktop
      // server is registered, so file upload works — don't warn the user.
      if (_mcpAlreadyExists(err)) {
        added.push("ainxt_desktop");
        _trace("MCP_INJECT", "[legacy] ainxt_desktop already registered — reusing existing entry");
      } else {
        _trace("MCP_INJECT_ERROR", `[legacy] ainxt_desktop: ${err.message}`);
        // LOUD FAILURE: without ainxt_desktop the `upload_file_to_chat` tool is gone,
        // so Buddy cannot attach local folder files — tell the user instead of
        // letting the agent improvise an apology mid-send.
        this._desktopToolsUnavailable = true;
        this.emit(this.id, {
          type: "notice", level: "warn",
          msg: "Local file upload is unavailable this session — Buddy can't attach files "
             + "from your folder. Restart Buddy; if it persists, sign out and back in.",
        });
      }
    }
    this._injectedMcpServers = added;
  }

  // ── OLD CLI single-shot turn: spawn `--json`, capture one object, emit ─────
  _runLegacy(task, model) {
    const bin = resolveCliBinary();
    if (!bin) { this.emit(this.id, { type: "error", msg: "AiNxt couldn't start." }); return false; }
    this.busy = true;
    this._interrupted = false; // set by interrupt(); tells close() this was a user stop
    this.lastUsedAt = Date.now();
    // Remember the model this turn actually ran with so the NEXT turn (and any
    // ACP respawn logic) sees it as current, even if the caller only passes it
    // on some calls.
    if (model) this._currentModel = model;

    // Inject the office persona + memory into the message (same context builder
    // the ACP path uses) so the old CLI adopts the Buddy persona + folder listing.
    const content = this._buildTurnContent(task);
    const sys = this._systemPrompt();

    // Windows passes the WHOLE command line to CreateProcess as a single string
    // capped at 32,767 chars. The persona/system prompt + the injected memory +
    // folder listing + the user's task can easily blow past that → the spawn
    // throws "ENAMETOOLONG". So we keep the argv small and, when the payload is
    // large, feed the prompt (and, if needed, the system prompt) over STDIN
    // instead of on the command line. Small prompts keep the exact old behaviour.
    const CMDLINE_SAFE = 24000; // headroom under the 32,767 Windows limit
    const _legacyModel = model || this._currentModel || DEFAULT_CLI_MODEL;
    const baseArgs = [
      ...bin.args,
      "--json",
      ...(_legacyModel ? ["--model", _legacyModel] : []),
      ...(this._legacyGranted && this._legacyCwd ? ["--add-dir", this._legacyCwd] : []),
    ];

    // Projected command-line size if we passed everything on argv (the old way).
    const projected = [bin.command, ...baseArgs, "--append-system-prompt", sys, content]
      .join(" ").length;

    let args;
    let stdinPayload = null;
    if (projected <= CMDLINE_SAFE) {
      // Small enough: unchanged, prompt + system prompt on argv (as before).
      args = [...baseArgs, "--append-system-prompt", sys, content];
    } else if (([bin.command, ...baseArgs, "--append-system-prompt", sys].join(" ").length) <= CMDLINE_SAFE) {
      // System prompt still fits on argv; move only the (large) user prompt to stdin.
      args = [...baseArgs, "--append-system-prompt", sys];
      stdinPayload = content;
      _trace("SPAWN_STDIN", { reason: "prompt-oversized", contentLen: content.length, sysLen: sys.length });
    } else {
      // Both are large: fold the system prompt into the stdin prompt so NOTHING
      // oversized rides on the command line. The old CLI reads the prompt from
      // stdin when the positional prompt is omitted.
      args = [...baseArgs];
      stdinPayload =
        "<<BUDDY_OPERATING_CONTEXT>>\n" + sys + "\n<</BUDDY_OPERATING_CONTEXT>>\n\n" + content;
      _trace("SPAWN_STDIN", { reason: "prompt+sys-oversized", contentLen: content.length, sysLen: sys.length });
    }
    _trace("SPAWN", {
      protocol: "streamjson", mode: "--json turn", cwd: this._legacyCwd,
      projectedCmdline: projected, viaStdin: !!stdinPayload,
    });

    let out = "";
    let err = "";
    const proc = spawn(bin.command, args, {
      cwd: this._legacyCwd || process.cwd(),
      // When we pipe the prompt we must keep stdin open until we've written it;
      // otherwise stdin is inherited/ignored as before.
      stdio: stdinPayload ? ["pipe", "pipe", "pipe"] : ["ignore", "pipe", "pipe"],
      env: {
        ...process.env, FORCE_COLOR: "0", AINXT_IS_BUDDY: "1",
        ...TLS_ENV,
        ...(this.gatewayBase ? { AINXT_GATEWAY_URL: String(this.gatewayBase).replace(/\/+$/, "") } : {}),
        ...(this.jwt ? { AINXT_JWT: this.jwt } : {}),
      },
    });
    // Write the oversized prompt to stdin and close it so the CLI can start.
    if (stdinPayload && proc.stdin) {
      try { proc.stdin.write(stdinPayload); proc.stdin.end(); }
      catch (e) { _trace("STDIN_WRITE_FAILED", { message: e.message }); }
    }
    this.proc = proc; // so interrupt()/dispose() can kill the in-flight turn
    this.pid = proc.pid || null;
    proc.stdout.on("data", (d) => { out += d.toString(); });
    proc.stderr.on("data", (d) => {
      const t = d.toString();
      err += t;
      const line = t.trim();
      if (line) _trace("STDERR", line);
    });
    proc.on("error", (e) => {
      _trace("PROC_ERROR", { message: e.message });
      this.busy = false;
      this.emit(this.id, { type: "error", msg: "Something went wrong running AiNxt. Please try again." });
    });
    proc.on("close", (code) => {
      this.busy = false;
      this.proc = null;
      // User pressed Stop/Esc: this close is the result of us killing the turn.
      // Emit a clean "interrupted" result (NOT an error) so the UI leaves the
      // busy state without flashing a spurious "exited with code null" error.
      if (this._interrupted) {
        this._interrupted = false;
        this.emit(this.id, {
          type: "result", status: "interrupted", response: "", error: undefined,
          model: null, elapsedMs: 0, costUsd: 0, costTotalUsd: this._lastTotalCost,
          usage: { input: 0, output: 0 }, numTurns: 0,
        });
        return;
      }
      // The old CLI emits ONE JSON object {status,response,model,elapsed_ms}.
      // It may print a banner line first, so scan for the JSON object.
      let obj = null;
      const jsonStart = out.indexOf("{");
      if (jsonStart >= 0) {
        try { obj = JSON.parse(out.slice(jsonStart)); } catch { /* fall through */ }
      }
      if (obj && typeof obj.response === "string") {
        this.emit(this.id, { type: "token", text: obj.response });
        this.emit(this.id, {
          type: "result",
          status: obj.status === "ok" ? "ok" : "error",
          response: obj.response,
          model: obj.model || null,
          elapsedMs: obj.elapsed_ms || 0,
          costUsd: 0, costTotalUsd: this._lastTotalCost,
          usage: { input: 0, output: 0 },
          numTurns: 1,
          error: obj.status === "ok" ? undefined : (obj.response || "error"),
        });
        // Refresh memory after each turn (same as the ACP path).
        this._fetchMemoryPrompt().then((m) => { if (typeof m === "string") this._memoryPrompt = m; }).catch(() => {});
      } else {
        const detail = (err.trim() || out.trim() || `exited with code ${code}`).slice(0, 400);
        _trace("LEGACY_TURN_FAILED", { code, detail });
        this.emit(this.id, { type: "result", status: "error", response: "", error: detail, model: null, elapsedMs: 0, costUsd: 0, costTotalUsd: this._lastTotalCost, usage: { input: 0, output: 0 }, numTurns: 0 });
      }
    });
    return true;
  }

  // Shared context builder — persona + memory injection (used by BOTH paths).
  _buildTurnContent(task) {
    let content = task;
    if (!this._contextInjected) {
      this._contextInjected = true;
      this._memoryInjected = this._memoryPrompt || "";
      const ctx = this._systemPrompt();
      if (ctx && ctx.trim()) {
        content =
          "<<BUDDY_OPERATING_CONTEXT>>\n" +
          "The following defines who you are and what you already know about this user. " +
          "Treat it as your system instructions for this ENTIRE conversation. In particular, " +
          "the remembered facts below are things you ALREADY KNOW — never claim you have no memory.\n\n" +
          ctx +
          "\n<</BUDDY_OPERATING_CONTEXT>>\n\n" +
          task;
      }
    } else if ((this._memoryPrompt || "") !== (this._memoryInjected || "")) {
      this._memoryInjected = this._memoryPrompt || "";
      if (this._memoryPrompt && this._memoryPrompt.trim()) {
        content =
          "<<BUDDY_UPDATED_MEMORY>>\n" +
          "Your memory about this user was just updated — these are things you ALREADY KNOW. " +
          "Use them; never claim you don't know them.\n\n" +
          this._memoryPrompt +
          "\n<</BUDDY_UPDATED_MEMORY>>\n\n" +
          task;
      }
    }
    return content;
  }

  async _spawn() {
    const bin = resolveCliBinary();
    if (!bin) {
      this.emit(this.id, { type: "error", msg: "AiNxt couldn't start. Please reinstall the application or contact your administrator." });
      this.emit(this.id, { type: "session:exit", code: -1 });
      return Promise.resolve(false);
    }

    // ── SANDBOX: folder-scoped file access ────────────────────────────────
    // The agent's file access is scoped to the folder the user granted. We do
    // the same: when the user grants a working folder, `Read` is allowed but
    // confined to that folder (cwd + --add-dir). When NO folder is granted, the
    // agent gets NO local filesystem at all — it must work purely through the
    // gateway connectors/documents (never the desktop app's own directory).
    const granted = !!(this.cwd && fs.existsSync(this.cwd));
    // On a model-switch respawn (_respawning) reuse the SAME scratch dir instead
    // of minting a new temp folder each time — otherwise every switch would leak
    // the previous respawn's scratch dir (cleanup is skipped on purpose for a
    // respawn's close event; see proc.on("close") above).
    const cwd = granted
      ? this.cwd
      : (this._scratchDir && fs.existsSync(this._scratchDir) ? this._scratchDir : fs.mkdtempSync(path.join(os.tmpdir(), "ainxt-buddy-")));
    if (!granted) this._scratchDir = cwd; // tracked so close()/dispose() can clean it up
    // The hard file-read boundary enforced in the permission handler below: the
    // agent may ONLY read inside this folder (its real, resolved path). null =
    // no folder granted → no local reads at all.
    this._grantedRoot = granted ? fs.realpathSync(cwd) : null;
    // The office agent has NO directory-listing tool (removed for security), so
    // give it VISIBILITY of its project folder by listing the files here (scoped
    // to the granted folder, capped) and injecting them into the prompt.
    this._projectFolderListing = granted ? this._listProjectFolder(this._grantedRoot) : [];

    // ── OLD CLI (v1.0.2-beta): persistent `--full` stream-json agent ────────
    // The OLD build's proven persistent stream-json machinery: a long-running
    // `--full --print --input-format stream-json --output-format stream-json
    // --verbose --include-partial-messages ... --mcp-config <file>` process that
    // DOES load the connector MCP tools (the bare `--json` single-shot loaded
    // ZERO). cwd/granted (+ _grantedRoot / _projectFolderListing computed above)
    // are used by _spawnStreamLegacy for --add-dir + the folder boundary.
    if (this._protocol === "streamjson") {
      this._legacyCwd = cwd;
      this._legacyGranted = granted;
      return this._spawnStreamLegacy(cwd, granted);
    }

    // ── NEW CLI (ACP): persistent `agent stdio` process ────────────────────
    const args = this._spawnArgsAcp(cwd);
    // ACP v0.2.101 reads config.toml at launch; register MCP servers there
    // BEFORE spawning so the CLI picks them up on startup. Also inject the
    // x-ainxt-surface: buddy header so the gateway tags model_usages rows
    // with source_channel=DESKTOP-BUDDY instead of CLI, and x-ainxt-conv-id
    // so the gateway's Redis-backed Buddy history pipeline can key on the
    // durable conversation id from the very first inference request.
    this._injectSurfaceHeader();
    if (this._convId) this._injectConvIdHeader(this._convId);
    this._injectMcpIntoConfig();

    // Wait 500ms after MCP wiring before spawning.
    // - The OS needs time to flush config.toml (ACP) / the mcp-config file to disk.
    // - More importantly: ainxt_desktop (local HTTP MCP) needs to be fully
    //   listening before the CLI spawns and tries to connect. The CLI connects
    //   to all MCP servers in parallel during startup; if ainxt_desktop isn't
    //   ready yet, the connection silently fails and upload_file_to_chat is never
    //   available. 500ms is imperceptible vs the 3-5s total session startup time.
    await new Promise((r) => setTimeout(r, 500));

    _trace("SPAWN", { binary: bin.command, cwd, args });
    this.proc = spawn(bin.command, args, {
      cwd,
      // detached: put the CLI (and any children it spawns) in its OWN process group
      // so dispose() can kill the WHOLE group (kill(-pid)) — otherwise a crashed
      // Electron main orphans the CLI + its sub-processes, which keep billing the
      // gateway. We still track the pid and reap orphans on next startup.
      detached: process.platform !== "win32",
      // Pin the gateway URL + the VALIDATED token into the process env so EVERY
      // Anthropic SDK client in the tree — the main loop AND spawned sub-agents —
      // routes through the AiNxt gateway. Without this, a sub-agent fell back to the
      // real Anthropic API with the AiNxt model name ("claude-sonnet-4-6") → 404
      // "model not found". Using this.jwt (resolveValidToken) also avoids the stale
      // config.json token that `ainxt login` keeps resetting.
      env: {
        ...process.env, FORCE_COLOR: "0", AINXT_IS_BUDDY: "1",
        ...TLS_ENV,
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
      if (text) {
        _trace("STDERR", text);
        this.emit(this.id, { type: "notice", msg: text, level: "info" });
      }
    });
    this.proc.on("error", (err) => {
      _trace("PROC_ERROR", { message: err.message, code: err.code });
      this.emit(this.id, { type: "error", msg: "Something went wrong starting AiNxt. Please try again or reinstall the application." });
    });
    this.proc.on("spawn", () => {
      _trace("PROC_SPAWN", `process started successfully (protocol=${this._protocol})`);
      // Only the ACP path reaches here (streamjson early-returns without a
      // persistent process). Run the ACP handshake before marking ready so
      // sessionId is set before the first prompt.
      this._initialize().then(() => {
        _trace("INIT_OK", "handshake complete, session ready");
        this.ready = true;
        this._readyResolvers.forEach((r) => r(true));
        this._readyResolvers = [];
      }).catch((err) => {
        _trace("INIT_FAILED", err.message);
        this.ready = false;
        this._readyResolvers.forEach((r) => r(false));
        this._readyResolvers = [];
        this.emit(this.id, { type: "error", msg: `Buddy session init failed: ${err.message}` });
        // Emit session:exit so the UI marks this session as dead (conv.status="exited").
        // Without this, conv.status stays "idle" and ensureChatSession keeps reusing
        // the broken session — every subsequent message spins forever with no response.
        // Set _exitEmitted so proc.on("close") and dispose() don't emit a second/third
        // session:exit — the reducer is idempotent but persistCurrent() would fire 3×.
        this._exitEmitted = true;
        this.emit(this.id, { type: "session:exit", code: 1 });
      });
    });
    this.proc.on("close", (code) => {
      this.ready = false;
      this.busy = false;
      // setModel() (ACP model-switch respawn) killed this process on PURPOSE and
      // is about to _spawn() a new one — this is an internal restart, not a real
      // session end. Skip session:exit/MCP-cleanup so the UI doesn't flash
      // "exited" and the connectors config isn't torn down mid-switch; setModel()
      // itself drives the respawn's own ready/error signalling.
      if (this._respawning) return;
      this._cleanupMcpConfig();
      // Do NOT call _removeMcpFromConfig() here.
      // config.toml is shared across all sessions. Removing MCP entries on close
      // wipes the config for any other active session (race condition).
      // ainxt mcp add is idempotent — entries are re-written on next start.
      // Guard against double session:exit — INIT_FAILED already emitted one and
      // set _exitEmitted=true; proc.on("close") fires later when the process
      // actually exits. Without this guard persistCurrent() fires twice.
      if (!this._exitEmitted) {
        this._exitEmitted = true;
        this.emit(this.id, { type: "session:exit", code });
      }
      this._readyResolvers.forEach((r) => r(false));
      this._readyResolvers = [];
    });

    return new Promise((resolve) => {
      if (this.ready) return resolve(true);
      this._readyResolvers.push(resolve);
    });
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // OLD CLI (streamjson): persistent `--full` stream-json agent machinery.
  // Ported from the proven OLD build. This path spawns a LONG-RUNNING process
  // over the stream-json protocol WITH the connector MCP wired
  // via --mcp-config, so the agent actually loads the office/connector tools.
  // Entirely SEPARATE from the ACP path — the ACP _spawnArgsAcp /
  // _initializeAcp / _handleSdkMessage / _writeRpc* / _writeLine / _onStdout are
  // untouched.
  // ═══════════════════════════════════════════════════════════════════════════

  // Spawn the persistent OLD `--full` stream-json agent. Ported from OLD _spawn.
  async _spawnStreamLegacy(cwd, granted) {
    const bin = resolveCliBinary();
    if (!bin) {
      this.emit(this.id, { type: "error", msg: "AiNxt couldn't start. Please reinstall the application or contact your administrator." });
      this.emit(this.id, { type: "session:exit", code: -1 });
      return Promise.resolve(false);
    }

    // FULL-POWER MODE: expose the complete local-agent toolset with NO restrictions
    // (matches the Code tab). OFFICE MODE (default): dev tools stripped, Read
    // confined to the granted folder.
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
      // ── BUDDY ≠ CODE ────────────────────────────────────────────────────
      // Buddy is a NON-engineering office assistant: strip the dev tool surface.
      // Office tools (connectors, document generation, computer-use, browser)
      // arrive via the injected MCP servers (--mcp-config), not the built-in set.
      "--disallowedTools", ...disallowed,
      // Office isolation: load ONLY user settings — never project/local, so the
      // CLI can't walk cwd → home and auto-inject a stray AINXT.md into context.
      "--setting-sources", "user",
      "--permission-prompt-tool", "stdio",
      // Pre-allow ONLY harmless planning + delegation + the SAFE office tools
      // (doc generation, sandboxed run_code, KB retrieve, memory). `Read` is
      // deliberately NOT pre-allowed (it flows through can_use_tool where the
      // folder boundary is hard-enforced). Connector SENDS + Read + computer-use
      // are NOT pre-allowed and still flow through can_use_tool / native confirms.
      "--allowedTools",
      "TodoWrite", "Task",
      "mcp__ainxt_buddy__get_document_skill",
      "mcp__ainxt_buddy__build_document",
      "mcp__ainxt_buddy__list_document_versions",
      "mcp__ainxt_buddy__revise_artifact",
      "mcp__ainxt_buddy__analyze_data",
      "mcp__ainxt_buddy__deep_research",
      "mcp__ainxt_buddy__generate_document",
      "mcp__ainxt_buddy__run_code",
      "mcp__ainxt_buddy__retrieve",
      "mcp__ainxt_buddy__remember",
      "mcp__ainxt_desktop__list_files",
      // FULL-POWER: pre-allow the local/dev tools too so they run WITHOUT
      // per-action confirm. In office mode these aren't added.
      ...(this.devTools ? [
        "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep",
        "Bash", "NotebookEdit", "NotebookRead", "WebFetch", "WebSearch",
      ] : []),
      // File access: full-power → whole filesystem (root); office → granted folder only.
      ...(this.devTools ? ["--add-dir", path.parse(process.cwd()).root]
          : (granted ? ["--add-dir", cwd] : [])),
    ];
    // NOTE: we deliberately DO NOT psss --append-system-prompt. The office/persona
    // context is injected into the FIRST user turn via _buildTurnContent — the OLD
    // `--full` headless mode drops --append-system-prompt anyway, and omitting it
    // also avoids the Windows ENAMETOOLONG command-line limit.

    // Inject the gateway connector MCP (sse + Bearer JWT) — the P0 bridge that
    // makes connector/KB/doc tools available (the whole point of this path).
    // Also inject x-ainxt-surface: buddy and x-ainxt-conv-id into config.toml
    // [models].extra_headers so the gateway tags model_usages.source_channel as
    // DESKTOP-BUDDY and the Redis-backed Buddy history pipeline can key on the
    // durable conversation id. Both headers are read by the persistent --full
    // process at startup — they must be written BEFORE spawn.
    this._injectSurfaceHeader();
    if (this._convId) this._injectConvIdHeader(this._convId);
    const mcpConfig = this._writeMcpConfig();
    if (mcpConfig) args.push("--mcp-config", mcpConfig);

    if (this.resumeId) args.push("--resume", this.resumeId);

    // Give the desktop local MCP time to be listening before the CLI connects to
    // all MCP servers in parallel on startup (otherwise ainxt_desktop / file
    // upload can silently fail). Imperceptible vs the total session startup time.
    if (this.localMcpPort) await new Promise((r) => setTimeout(r, 500));

    _trace("SPAWN", { protocol: "streamjson", mode: "--full stream-json", binary: bin.command, cwd, args });
    this.proc = spawn(bin.command, args, {
      cwd,
      // detached: own process group so dispose() can kill the WHOLE tree (kill(-pid)).
      detached: process.platform !== "win32",
      env: {
        ...process.env, FORCE_COLOR: "0", AINXT_IS_BUDDY: "1",
        ...TLS_ENV,
        ...(this.gatewayBase ? { AINXT_GATEWAY_URL: String(this.gatewayBase).replace(/\/+$/, "") } : {}),
        ...(this.jwt ? { AINXT_JWT: this.jwt } : {}),
      },
    });
    this.pid = this.proc.pid || null;
    // Record the live pid on disk so a crash-restart can reap orphans.
    try { require("./pidRegistry").record(this.pid); } catch { /* optional */ }

    this.proc.stdout.setEncoding("utf-8");
    this.proc.stdout.on("data", (chunk) => this._onStdoutStream(chunk));
    this.proc.stderr.on("data", (d) => {
      const text = d.toString().trim();
      if (text) {
        _trace("STDERR", text);
        this.emit(this.id, { type: "notice", msg: text, level: "info" });
      }
    });
    this.proc.on("spawn", () => {
      _trace("PROC_SPAWN", "streamjson process started successfully");
      this.ready = true;
      this._readyResolvers.forEach((r) => r(true));
      this._readyResolvers = [];
      this._initializeStream();
    });
    this.proc.on("close", (code) => {
      this.ready = false;
      this.busy = false;
      this._cleanupMcpConfig();
      // User pressed Stop/Esc: this close is the result of us killing the turn.
      // Emit a clean "interrupted" result (NOT a session:exit) so the UI leaves
      // the busy state without flashing a spurious error — the same safety-net
      // shape the current streamjson interrupt uses.
      if (this._interrupted) {
        this._interrupted = false;
        this.emit(this.id, {
          type: "result", status: "interrupted", response: "", error: undefined,
          model: null, elapsedMs: 0, costUsd: 0, costTotalUsd: this._lastTotalCost,
          usage: { input: 0, output: 0 }, numTurns: 0,
        });
        return;
      }
      this.emit(this.id, { type: "session:exit", code });
      this._readyResolvers.forEach((r) => r(false));
      this._readyResolvers = [];
    });
    this.proc.on("error", (err) => {
      _trace("PROC_ERROR", { message: err.message, code: err.code });
      this.emit(this.id, { type: "error", msg: "Something went wrong starting AiNxt. Please try again or reinstall the application." });
    });

    return new Promise((resolve) => {
      if (this.ready) return resolve(true);
      this._readyResolvers.push(resolve);
    });
  }

  // Persistent streamjson stdout handler — buffer, split on \n, JSON.parse each
  // line, dispatch to _handleStreamMessage. Ignores non-JSON diagnostics.
  _onStdoutStream(chunk) {
    this._stdoutBuf += chunk;
    let nl;
    while ((nl = this._stdoutBuf.indexOf("\n")) >= 0) {
      const line = this._stdoutBuf.slice(0, nl).trim();
      this._stdoutBuf = this._stdoutBuf.slice(nl + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); }
      catch { _trace("STDOUT_NOT_JSON", line); continue; }
      _trace("STDOUT_RAW", line);
      this._handleStreamMessage(msg);
    }
  }

  // OLD stream-json SDK message handler. Ported VERBATIM from OLD _handleSdkMessage
  // EXCEPT the permission block (control_request / can_use_tool), where the OLD
  // silent auto-approve for acceptEdits/bypass/non-destructive is intentionally
  // removed — Buddy always surfaces the Allow/Don't Allow prompt (except a tool
  // the user chose "Always allow" for this session, or the read boundary).
  _handleStreamMessage(msg) {
    switch (msg.type) {
      case "system":
        if (msg.subtype === "init") {
          if (msg.session_id) {
            // If we asked to --resume a specific session but the CLI came back with a
            // DIFFERENT session_id, the resume FAILED (the on-disk session was
            // pruned/expired) and it started fresh — flag it so the first turn
            // replays the saved transcript preamble and the user is told.
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
        // build_document). That can stream invisibly, so a long doc-gen looks frozen.
        // Stream a live char-count progress signal.
        else if (ev.type === "content_block_delta" && d && d.type === "input_json_delta" &&
                 typeof d.partial_json === "string") {
          this._streamChars = (this._streamChars || 0) + d.partial_json.length;
          if (this._streamChars - (this._lastProgressAt || 0) >= 400) {
            this._lastProgressAt = this._streamChars;
            this.emit(this.id, { type: "tool:progress", name: this._streamToolName || "", chars: this._streamChars });
          }
        }
        // A tool_use block is STARTING — emit an early signal with the tool name.
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
            // progress). A BATCH reuses a tool with DIFFERENT inputs and is
            // legitimate. Count consecutive calls with the same signature, plus a
            // high total-call backstop.
            const g = this._loopGuard;
            let sig = b.name;
            try { sig += "::" + JSON.stringify(b.input).slice(0, 300); } catch { /* ignore */ }
            g.totalCalls += 1;
            if (sig === g.lastSig) g.sameCount += 1;
            else { g.lastSig = sig; g.sameCount = 1; }
            if (!this._loopTripped && (g.sameCount > _LOOP_SAME_COUNT_THRESHOLD || g.totalCalls > _LOOP_TOTAL_CALLS_THRESHOLD)) {
              this._loopTripped = true;
              _trace("LOOP_TRIPPED", { name: b.name, sameCount: g.sameCount, totalCalls: g.totalCalls, isExtractionTool: _isExtractionTool(b.name), input: b.input });
              this.emit(this.id, { type: "error", msg: _loopTripMessage(b.name) });
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
          // Buddy may only read inside the granted project folder. Auto-allow if
          // inside, auto-DENY if outside (or if no folder granted). Enforced HERE.
          const boundary = this._readBoundaryDecision(req.tool_name, req.input || {});
          if (boundary === "allow") {
            this._respondControlStream(msg.request_id, { behavior: "allow", updatedInput: req.input || {} });
            return;
          }
          if (boundary === "deny") {
            this._respondControlStream(msg.request_id, {
              behavior: "deny",
              message: this._grantedRoot
                ? `Reading outside this project's folder is not allowed. You may only read files inside ${this._grantedRoot}.`
                : "No project folder is attached, so local files can't be read. Use your connectors or documents, or ask the user to attach a folder.",
            });
            return;
          }
          // A tool the user chose "Always allow" for THIS session — an explicit
          // user decision, the ONLY auto-approve path. (The OLD silent
          // acceptEdits/bypass/non-destructive auto-approve is intentionally removed.)
          if (this._alwaysAllowTools && this._alwaysAllowTools.has(req.tool_name)) {
            this._respondControlStream(msg.request_id, { behavior: "allow", updatedInput: req.input || {} });
            return;
          }
          // Everything else → user confirmation.
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
        // Refresh durable memory after each turn so a fact JUST saved via
        // `remember` is available on the NEXT turn.
        this._fetchMemoryPrompt().then((m) => { if (typeof m === "string") this._memoryPrompt = m; }).catch(() => {});
        return;
      }

      default:
        return; // keep_alive, etc.
    }
  }

  // Write a stream-json object to the persistent process's stdin.
  _writeStream(obj) {
    if (this.proc && this.proc.stdin && this.proc.stdin.writable) {
      const line = JSON.stringify(obj);
      _trace("STDIN_SEND", line);
      this.proc.stdin.write(line + "\n");
    } else {
      _trace("STDIN_BLOCKED", { reason: !this.proc ? "no proc" : "stdin not writable" });
    }
  }

  // Send a control_response (allow/deny) for a can_use_tool request WITHOUT
  // bothering the user — used to auto-enforce the read boundary + Always-allow.
  _respondControlStream(requestId, response) {
    this._pendingPerms.delete(requestId);
    this._writeStream({ type: "control_response", response: { subtype: "success", request_id: requestId, response } });
  }

  // Send a control_request to the CLI and await its control_response.
  _sendControlStream(request) {
    return new Promise((resolve, reject) => {
      if (!this.proc || !this.ready) return reject(new Error("session not ready"));
      const request_id = randomUUID();
      this._pendingCtrl.set(request_id, { resolve, reject });
      this._writeStream({ type: "control_request", request_id, request });
      setTimeout(() => {
        if (this._pendingCtrl.has(request_id)) {
          this._pendingCtrl.delete(request_id);
          reject(new Error("control_request timed out"));
        }
      }, 15000);
    });
  }

  // stream-json handshake: initialize control_request → publish slash commands.
  async _initializeStream() {
    try {
      const resp = await this._sendControlStream({ subtype: "initialize" });
      const cmds = Array.isArray(resp && resp.commands) ? resp.commands : [];
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

  // Run a turn on the persistent stream-json agent: inject the office persona +
  // memory into the message (via _buildTurnContent) and write a `user` message.
  _runStreamLegacy(task) {
    this.busy = true;
    this.lastUsedAt = Date.now();   // keep this session at the front of the LRU
    this._deltasSinceAssistant = false;
    this._loopGuard = { lastSig: null, sameCount: 0, totalCalls: 0 };
    this._loopTripped = false;
    const content = this._buildTurnContent(task);
    this._writeStream({ type: "user", message: { role: "user", content } });
    return true;
  }

  _onStdout(chunk) {
    // Split into lines immediately. Last element is an incomplete line — carry forward.
    const allLines = (this._stdoutBuf + chunk).split("\n");
    this._stdoutBuf = allLines.pop() ?? "";

    allLines.forEach((rawLine) => {
      const line = rawLine.trim();
      if (!line) return;
      let msg;
      try { msg = JSON.parse(line); }
      catch { _trace("STDOUT_NOT_JSON", line); return; }
      _trace("STDOUT_RAW", line);
      this._handleSdkMessage(msg);
    });
  }

  // Persistent-process stdout handler. Only the NEW CLI (ACP `agent stdio`) keeps
  // a long-running process piping NDJSON here; the OLD CLI is single-shot `--json`
  // and parses its one-object output inline in _runLegacy(), so this is ACP-only.
  _handleSdkMessage(msg) {
    // JSON-RPC response (has id, no method) — resolves a pending request
    if (msg.id !== undefined && msg.method === undefined) {
      const pend = this._pendingCtrl.get(msg.id);
      if (pend) {
        this._pendingCtrl.delete(msg.id);
        if (msg.error) pend.reject(new Error(msg.error.message || "rpc error"));
        else pend.resolve(msg.result || {});
      }
      // Final result for the current session/prompt turn
      if (msg.id === this._currentTurnId) {
        this.busy = false;
        this._deltasSinceAssistant = false;
        this._toolNames.clear();
        this._pendingPerms.clear();
        this._loopGuard = { lastSig: null, sameCount: 0, totalCalls: 0 };
        this._loopTripped = false;
        const r = msg.result || {};
        const meta = r._meta || {};
        const usage = (meta.usage) || {};
        const responseText = this._streamBuffer || (typeof r.text === "string" ? r.text : "");
        this._streamBuffer = "";
        // Derive cost from sessionCostTicks accumulated during streaming chunks.
        // The CLI sends its cumulative session cost as ticks on every chunk _meta;
        // dividing by 1e11 gives the exact USD value the CLI TUI displays.
        // turnCost = session total so far minus what prior turns already reported.
        // Falls back to 0 when no ticks were received (e.g. non-streaming turns).
        const sessionCostUsd = this._lastSessionCostTicks / 1e11;
        const turnCost = Math.max(0, sessionCostUsd - this._lastTotalCost);
        const totalCost = this._lastTotalCost + turnCost;
        this._lastTotalCost = totalCost;
        this._lastSessionCostTicks = 0; // reset for next turn
        const inTok  = usage.inputTokens  || meta.inputTokens  || 0;
        const outTok = usage.outputTokens || meta.outputTokens || 0;
        // Document/tool-only turns: the CLI sends a session/prompt result with
        // totalTokens=0 AFTER the real result was already delivered via
        // _ainxt.dev/session_notification turn_completed. Skip this ghost emit —
        // it would show ↑0 ↓0 tok $0.00 after every document build.
        // Text turns always have tokens > 0 so they are never skipped.
        const totalTok = meta.totalTokens || (inTok + outTok);
        if (totalTok === 0 && !responseText && !msg.error) {
          this._reportUsage(0, { input_tokens: 0, output_tokens: 0 });
          return;
        }
        // When tokens are 0 (document follow-up text, tool-only sub-turns),
        // emit null for cost/tokens so MessageMeta hides the badge entirely
        // via its existing null checks — avoids showing ↑0 ↓0 tok $0.00.
        const hasTokens = inTok > 0 || outTok > 0;
        this.emit(this.id, {
          type: "result",
          status: msg.error ? "error" : "ok",
          response: responseText,
          model: meta.modelId || r.model || null,
          elapsedMs: usage.apiDurationMs || r.duration_ms || 0,
          costUsd:      hasTokens ? turnCost  : null,
          costTotalUsd: hasTokens ? totalCost : this._lastTotalCost,
          usage: { input: hasTokens ? inTok : null, output: hasTokens ? outTok : null },
          numTurns: usage.numTurns || r.num_turns || 0,
          error: msg.error ? (msg.error.message || "error") : undefined,
        });
        // Report usage to gateway analytics (fire-and-forget).
        this._reportUsage(hasTokens ? turnCost : 0, { input_tokens: inTok, output_tokens: outTok });
        // Emit context usage so the UI context bar stays up to date.
        this.getContextUsage().then((d) => {
          if (d && typeof d.percentage !== "undefined") {
            this.emit(this.id, { type: "context", pct: d.percentage, tokens: d.totalTokens, max: d.rawMaxTokens });
          }
        }).catch(() => {});
        // Refresh memory after each turn so newly saved facts are available next turn.
        this._fetchMemoryPrompt().then((m) => { if (typeof m === "string") this._memoryPrompt = m; }).catch(() => {});
      }
      return;
    }

    const method = msg.method || "";
    const params = msg.params || {};

    switch (method) {
      // ── Streaming text ──────────────────────────────────────────────────
      case "session/update": {
        const update = params.update || {};

        // Streaming token (agent_message_chunk = v0.2.101, assistant_message_chunk = older)
        if ((update.sessionUpdate === "agent_message_chunk" || update.sessionUpdate === "assistant_message_chunk") && update.content) {
          const text = typeof update.content === "string" ? update.content : (update.content.text || "");
          if (text) {
            this._deltasSinceAssistant = true;
            this._streamBuffer += text;
            this.emit(this.id, { type: "token", text });
          }
          // Track the CLI's running session cost from the outer message _meta.
          // sessionCostTicks ÷ 1e11 = USD — exact same value the CLI TUI shows.
          // The CLI updates its own rate table so this stays correct for all
          // models without any hardcoding on the desktop side.
          const costTicks = params._meta?.sessionCostTicks;
          if (typeof costTicks === "number" && costTicks > 0) {
            this._lastSessionCostTicks = costTicks;
          }
        }
        // Tool starting (tool_call = v0.2.101, tool_use_start = older)
        else if (update.sessionUpdate === "tool_call") {
          const wrapperName = update.title || "tool";
          const rawInput = update.rawInput || {};
          // Unwrap use_tool/search_tool to show the REAL tool being called
          // (e.g. "ainxt_buddy__people_search") instead of the generic wrapper
          // name — see _realToolName for why this indirection exists.
          const name = _realToolName(wrapperName, rawInput);
          // The actual tool's arguments live under rawInput.tool_input for a
          // use_tool call; fall back to rawInput itself for anything else.
          const innerInput = (rawInput && rawInput.tool_input) || rawInput;
          const toolId = update.toolCallId;
          if (toolId) this._toolNames.set(toolId, name);
          // ── Circuit breaker (cost protection) ──────────────────────────
          const g = this._loopGuard;
          let sig = name;
          try { sig += "::" + JSON.stringify(innerInput).slice(0, 300); } catch { /* ignore */ }
          g.totalCalls += 1;
          if (sig === g.lastSig) g.sameCount += 1;
          else { g.lastSig = sig; g.sameCount = 1; }
          if (!this._loopTripped && (g.sameCount > _LOOP_SAME_COUNT_THRESHOLD || g.totalCalls > _LOOP_TOTAL_CALLS_THRESHOLD)) {
            this._loopTripped = true;
            _trace("LOOP_TRIPPED", { name, sameCount: g.sameCount, totalCalls: g.totalCalls, isExtractionTool: _isExtractionTool(name), input: innerInput });
            this.emit(this.id, { type: "error", msg: _loopTripMessage(name) });
            try { this.interrupt(); } catch { /* ignore */ }
            return;
          }
          // ── Block onedrive_upload / base64 file-read loops for local paths ──
          // The AI sometimes tries to upload local folder files via onedrive_upload
          // (base64-encoding them first). This never works — block it early and tell
          // the user to use the paperclip button instead.
          const toolNameLower = name.toLowerCase();
          const isOnedriveUpload = toolNameLower.includes("onedrive_upload") || toolNameLower.includes("onedrive__upload");
          const uploadPath = innerInput && (innerInput.file_path || innerInput.path || innerInput.content || "");
          const isLocalPath = typeof uploadPath === "string" && (uploadPath.match(/^[A-Za-z]:[\\\/]/) || uploadPath.startsWith("D:\\") || uploadPath.startsWith("C:\\"));
          if (isOnedriveUpload && isLocalPath) {
            this.emit(this.id, { type: "token", text: "\n\n⚠️ **Cannot use OneDrive upload for local files.** Use `upload_file_to_chat` instead — it uploads the file directly from the attached folder and returns an `attachment_id` you can pass to the send tool.\n" });
            try { this.interrupt(); } catch { /* ignore */ }
            return;
          }
          // Emit tool:preparing first (shows "Preparing…" in UI before input streams in)
          this.emit(this.id, { type: "tool:preparing", name });
          this.emit(this.id, { type: "tool:start", name, detail: toolDetail(innerInput), diff: buildDiff(name, innerInput) });
        }
        // tool_call_delta_chunk — streams the tool's input arguments character by character.
        // Emit tool:progress so the UI shows a live char-count for long doc-gen scripts.
        else if (update.sessionUpdate === "tool_call_delta_chunk" && update.arguments_delta) {
          this._streamChars = (this._streamChars || 0) + update.arguments_delta.length;
          if (this._streamChars - (this._lastProgressAt || 0) >= 400) {
            this._lastProgressAt = this._streamChars;
            const toolId = update.tool_call_id;
            const name = (toolId && this._toolNames.get(toolId)) || "";
            this.emit(this.id, { type: "tool:progress", name, chars: this._streamChars });
          }
        }
        else if (update.sessionUpdate === "tool_use_start" && update.tool) {
          const name = update.tool.name || "tool";
          if (update.tool.id) this._toolNames.set(update.tool.id, name);
          this.emit(this.id, { type: "tool:start", name, detail: toolDetail(update.tool.input), diff: buildDiff(name, update.tool.input) });
        }
        // Tool completed (tool_call_update = v0.2.101, tool_use_result = older)
        else if (update.sessionUpdate === "tool_call_update") {
          const toolId = update.toolCallId;
          const name = (toolId && this._toolNames.get(toolId)) || update.title || "tool";
          const status = (update.status || "").toLowerCase();
          if (status === "completed") this.emit(this.id, { type: "tool:done", name });
          else if (status === "failed" || status === "error") {
            // Surface WHY it failed — the reason is buried in rawOutput.message /
            // content[0].content.text and was previously dropped entirely, so a
            // failed tool call couldn't be diagnosed (e.g. "Tool not found: ..."
            // vs a real connector/auth error) without a live desktop console.
            // LOGGED ONLY — deliberately NOT surfaced in the UI (detail omitted
            // from the emitted event); the chip stays a plain red X for the user,
            // the reason is for support/diagnostics via buddy-trace.log only.
            const rawOut = update.rawOutput || {};
            const contentText = Array.isArray(update.content) && update.content[0]
              ? (update.content[0].content && update.content[0].content.text) || ""
              : "";
            const reason = rawOut.message || contentText || "(no error detail provided by CLI)";
            _trace("TOOL_CALL_FAILED", { name, toolId, reason });
            this.emit(this.id, { type: "tool:fail", name });
          }
        }
        else if (update.sessionUpdate === "tool_use_result" && update.tool_use_id) {
          const name = this._toolNames.get(update.tool_use_id) || "tool";
          this.emit(this.id, { type: update.is_error ? "tool:fail" : "tool:done", name });
        }
        // Turn complete
        else if (update.sessionUpdate === "turn_complete" || update.sessionUpdate === "assistant_turn_complete" || update.sessionUpdate === "turn_completed") {
          this.busy = false;
          this._deltasSinceAssistant = false;
          this._toolNames.clear();
          this._pendingPerms.clear();
          this._loopGuard = { lastSig: null, sameCount: 0, totalCalls: 0 };
          this._loopTripped = false;
          const responseText = this._streamBuffer;
          this._streamBuffer = "";
          // Derive cost from sessionCostTicks — same logic as the session/prompt
          // result handler. Document/tool-heavy turns fire turn_completed here
          // instead of going through the result handler, so cost must be computed
          // in both places. Falls back to 0 when no ticks received.
          const _tcSessionCostUsd = this._lastSessionCostTicks / 1e11;
          const _tcTurnCost = Math.max(0, _tcSessionCostUsd - this._lastTotalCost);
          this._lastTotalCost += _tcTurnCost;
          this._lastSessionCostTicks = 0;
          // Mark that this turn was already resolved here so the session/prompt
          // result handler (which fires shortly after for the same turn) skips
          // emitting a duplicate zero-cost/zero-token ghost message.
          this._turnCompletedEmitted = true;
          this.emit(this.id, {
            type: "result", status: update.error ? "error" : "ok",
            response: responseText, model: update.model || null,
            elapsedMs: update.duration_ms || 0,
            costUsd: _tcTurnCost, costTotalUsd: this._lastTotalCost,
            usage: { input: update.input_tokens || 0, output: update.output_tokens || 0 },
            numTurns: update.num_turns || 0, error: update.error || undefined,
          });
          this._fetchMemoryPrompt().then((m) => { if (typeof m === "string") this._memoryPrompt = m; }).catch(() => {});
          // A model switch was requested mid-turn (setModel() deferred it rather
          // than interrupting the in-flight turn) — apply it now that we're idle.
          if (this._pendingModelSwitch && this._pendingModelSwitch !== this._currentModel) {
            const next = this._pendingModelSwitch;
            this._pendingModelSwitch = null;
            this._currentModel = next;
            this._respawnAcp().catch(() => {});
          } else {
            this._pendingModelSwitch = null;
          }
        }
        // Slash commands / tools list update
        else if (update.sessionUpdate === "available_commands_update") {
          const cmds = Array.isArray(update.availableCommands) ? update.availableCommands : [];
          const tools = Array.isArray((update._meta || {}).tools) ? update._meta.tools : [];
          // ── Connector tool-catalog diagnostics ────────────────────────────
          // "M365 shows connected but tool calls fail / tools missing" is
          // undiagnosable from the desktop alone unless we can see EXACTLY which
          // connector tools the gateway handed the CLI for THIS session. Log a
          // single greppable summary line (enable with AINXT_CLI_TRACE=1) so a
          // support run can answer "was outlook_send_mail even in the list?"
          // without needing server-side log access.
          const buddy = tools.filter((t) => t.startsWith("ainxt_buddy__"));
          const m365 = buddy.filter((t) => /outlook|teams|calendar|onedrive|people_search/i.test(t));
          const byConnector = {};
          for (const t of buddy) {
            const rest = t.slice("ainxt_buddy__".length);
            const slug = rest.split("_")[0] || rest;
            byConnector[slug] = (byConnector[slug] || 0) + 1;
          }
          _trace("MCP_TOOL_CATALOG", {
            totalTools: tools.length,
            ainxtBuddyTools: buddy.length,
            ainxtDesktopTools: tools.filter((t) => t.startsWith("ainxt_desktop__")).length,
            microsoft365ToolsFound: m365.length,
            microsoft365Tools: m365,
            toolsByConnectorPrefix: byConnector,
          });
          if (m365.length === 0) {
            _trace(
              "MCP_TOOL_CATALOG_WARN",
              "No Outlook/Teams/Calendar tools in this session's tool list. " +
              "This means the gateway's connector registry found no ACTIVE Microsoft 365 " +
              "token for this user at MCP-init time — NOT a TLS/network issue (the MCP " +
              "connection itself succeeded, since we got a tool list at all). Check the " +
              "gateway's ainxt.user_oauth_tokens row for this user_id + 'microsoft_365'."
            );
          }
          this.emit(this.id, {
            type: "session:init", model: null, permissionMode: this._permMode || "default",
            slashCommands: cmds.map((c) => ({ name: c.name, description: c.description || "", argumentHint: (c.input && c.input.hint) || "" })),
            tools, skills: [],
          });
        }
        return;
      }

      // ── Permission / confirm request ────────────────────────────────────
      case "agent/confirmTool":
      case "agent/confirm": {
        const reqId = msg.id;
        const toolName = params.name || params.tool || "tool";
        const input = params.input || {};
        // Apply the same file-read boundary as before
        const boundary = this._readBoundaryDecision(toolName, input);
        if (boundary === "allow") {
          this._writeRpcResponse(reqId, { allowed: true });
          return;
        }
        if (boundary === "deny") {
          this._writeRpcResponse(reqId, { allowed: false, reason: this._grantedRoot
            ? `Reading outside this project's folder is not allowed.`
            : "No project folder is attached — use connectors or ask the user to attach a folder." });
          return;
        }
        // Silent auto-approve REMOVED: Buddy must always surface the Allow /
        // Don't Allow / Always allow prompt to the user. The ONLY exception is a
        // tool the user has already chosen "Always allow" for in THIS session —
        // that is an explicit user decision, not a silent default.
        if (this._alwaysAllowTools && this._alwaysAllowTools.has(toolName)) {
          this._writeRpcResponse(reqId, { allowed: true });
          return;
        }
        if (reqId !== undefined && reqId !== null) this._pendingPerms.set(reqId, { tool: toolName, input });
        const detail = toolDetail(input);
        this.emit(this.id, { type: "confirm", id: reqId, tool: toolName, detail, label: `Allow ${toolName}${detail ? `: ${detail}` : ""}?` });
        return;
      }

      // ── session/request_permission — tool execution gate (v0.2.101+) ──────
      // New CLI sends this instead of agent/confirm before write tools.
      // Response format: { optionId: "allow-once" | "reject-once" }
      // Show the same confirm dialog as agent/confirm so the user sees
      // Allow / Don't Allow — identical UX to the old CLI.
      // ── session/request_permission — tool execution gate (v0.2.101+) ──────
      // New CLI sends this instead of agent/confirm before executing tools.
      // Show the same Allow / Don't Allow dialog as the old CLI.
      // IMPORTANT: id is integer 0 — store it and use _writeLine (not
      // _writeRpcResponse) to preserve the integer type in the response.
      // Response struct: SelectedPermissionOutcome { optionId, kind }
      case "session/request_permission": {
        const reqId = msg.id;  // integer 0
        const toolCall = params.toolCall || {};
        const wrapperName = toolCall.title || toolCall.tool || "tool";
        // Unwrap use_tool/search_tool so the confirm dialog (and the
        // destructive-tool check that gates auto-allow) see the REAL tool —
        // e.g. "ainxt_buddy__teams_send_chat_message", not "use_tool".
        const toolName = _realToolName(wrapperName, toolCall.rawInput || {});
        const input = (toolCall.rawInput || {}).tool_input || toolCall.rawInput || {};
        const options = params.options || [];
        // Helper: reply "allow once" using the CLI's own option ids.
        // ACP RequestPermissionResponse { outcome: RequestPermissionOutcome }
        // where RequestPermissionOutcome is internally-tagged on "outcome":
        //   Selected  -> { outcome: "selected", optionId: <string> }
        //   Cancelled -> { outcome: "cancelled" }
        const _allowOnce = () => {
          const chosen = options.find((o) => o.kind === "allow_once")
            || options.find((o) => o.optionId === "allow-once")
            || { optionId: "allow-once" };
          this._writeLine({ jsonrpc: "2.0", id: reqId, result: { outcome: { outcome: "selected", optionId: chosen.optionId } } });
        };
        // 1) Hard file-read boundary (same as the old CLI): allow reads inside the
        //    granted folder, deny outside / when no folder is attached.
        const boundary = this._readBoundaryDecision(toolName, input);
        if (boundary === "allow") { _allowOnce(); return; }
        if (boundary === "deny") {
          const rej = options.find((o) => o.kind === "reject_once")
            || options.find((o) => o.optionId === "reject-once")
            || { optionId: "reject-once" };
          this._writeLine({ jsonrpc: "2.0", id: reqId, result: { outcome: { outcome: "selected", optionId: rej.optionId } } });
          return;
        }
        // 2) A tool the user already chose "Always allow" for this session.
        if (this._alwaysAllowTools && this._alwaysAllowTools.has(toolName)) { _allowOnce(); return; }
        // 3) Only SENSITIVE actions prompt — connector SENDS / destructive /
        //    state-changing tools, and local file WRITES. Everything else (reads,
        //    searches, document generation, KB retrieve, planning) runs without a
        //    prompt, matching the old CLI's UX where only writes/sends confirm.
        if (!_isSensitiveTool(toolName)) { _allowOnce(); return; }
        // Store with options so respondConfirm can pick the right optionId+kind
        if (reqId !== undefined && reqId !== null) {
          this._pendingPerms.set(reqId, { tool: toolName, input, options, isPermissionRequest: true });
        }
        const detail = toolDetail(input);
        this.emit(this.id, { type: "confirm", id: reqId, tool: toolName, detail, label: `Allow ${toolName}${detail ? `: ${detail}` : ""}?` });
        return;
      }

      // ── ask_user_question — auto-answer (no interactive UI) ─────────────
      case "_ainxt.dev/ask_user_question": {
        const reqId = msg.id;
        const questions = params.questions || [];
        const answers = questions.map((q) => {
          const first = (q.options && q.options[0]) ? q.options[0].label : "";
          return q.multiSelect ? [first] : first;
        });
        const summary = questions.map((q, i) => {
          const chosen = Array.isArray(answers[i]) ? answers[i].join(", ") : answers[i];
          return `**${q.question}**\n→ Auto-selected: *${chosen}*`;
        }).join("\n\n");
        this.emit(this.id, { type: "token", text: `\n\n📋 *The AI asked for your input — auto-answering with recommended options:*\n\n${summary}\n\n` });
        // ACP AskUserQuestionExtResponse is an internally-tagged enum on
        // "outcome". The success variant is:
        //   Accepted -> { outcome: "accepted", answers: [...], partial_answers: [...] }
        // (other variants: chat_about_this / skip_interview / cancelled).
        // The old shape { answers } was rejected by the new CLI with
        // "missing field `outcome`". Reply with the wrapped Accepted form.
        this._writeLine({ jsonrpc: "2.0", id: reqId, result: { outcome: "accepted", answers, partial_answers: [] } });
        return;
      }

      // ── exit_plan_mode — CLI waiting for plan approval ───────────────────
      // Auto-approve so the agent proceeds; show the plan content to the user.
      case "_ainxt.dev/exit_plan_mode": {
        const reqId = msg.id;
        const planContent = params.planContent || "";
        if (planContent) {
          this.emit(this.id, { type: "token", text: `\n\n📋 **Plan ready — proceeding:**\n\n${planContent}\n\n` });
        }
        this._writeRpcResponse(reqId, { approved: true });
        return;
      }

      // ── Turn complete notification (safety net) ─────────────────────────
      case "_ainxt.dev/session/prompt_complete": {
        if (this.busy) {
          this.busy = false;
          this._deltasSinceAssistant = false;
          this._toolNames.clear();
          this._pendingPerms.clear();
          const responseText = this._streamBuffer;
          this._streamBuffer = "";
          this.emit(this.id, { type: "result", status: "ok", response: responseText, model: null, elapsedMs: 0, costUsd: 0, costTotalUsd: this._lastTotalCost, usage: { input: 0, output: 0 }, numTurns: 0 });
          this._fetchMemoryPrompt().then((m) => { if (typeof m === "string") this._memoryPrompt = m; }).catch(() => {});
        }
        return;
      }

      // ── Benign notifications — ignore ───────────────────────────────────
      case "_ainxt.dev/mcp/servers_updated":
      case "_ainxt.dev/mcp_initialized":
      case "_ainxt.dev/sessions/changed":
      case "_ainxt.dev/queue/changed":
      case "_ainxt.dev/session_notification":
        return;

      default:
        _trace("UNHANDLED_MSG", `method=${method} params=${JSON.stringify(params).slice(0, 200)}`);
        return;
    }
  }

  // ── stream-json protocol handler ─────────────────────────────────────────
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

  run({ task, model, convId }) {
    if (!this.ready) {
      // Emit result:error so the UI spinner clears (conv.status → "error"),
      // then emit session:exit so conv.status → "exited". The "exited" status
      // is critical: ensureChatSession rejects "exited" sessions and spawns a
      // fresh one, which breaks the auto-dequeue loop that would otherwise fire
      // every 150ms (result:error → dequeue → run(!ready) → result:error → ...).
      this.emit(this.id, { type: "result", status: "error", error: "Session is not ready — please start a new conversation." });
      this.emit(this.id, { type: "session:exit", code: 1 });
      return false;
    }
    if (this.busy) {
      this.emit(this.id, { type: "error", msg: "Session is busy — wait for the current turn to finish or interrupt it." });
      return false;
    }

    // Update the stored convId and inject it into config.toml so:
    //   1. Any future respawn (model switch, idle eviction) picks up the current
    //      conversation id at spawn time (the primary path for persistent CLIs).
    //   2. Brand-new conversations (convId not known at create time) get the
    //      header written before the first turn reaches the gateway.
    // For already-running persistent processes (streamjson --full, ACP) the
    // config.toml write is a no-op for the current process (it already read
    // config.toml at startup) but ensures the NEXT respawn is correct.
    if (convId && convId !== this._convId) {
      this._convId = convId;
      this._injectConvIdHeader(convId);
    }

    // ── OLD CLI: persistent `--full` stream-json turn ──────────────────────
    // No live model-switch here: the persistent process is spawned once and the
    // old CLI has no per-turn --model flag on this path (see _spawnStreamLegacy).
    // A model change is picked up by setModel() on the NEXT session respawn.
    if (this._protocol === "streamjson") {
      if (!this.proc) return false;
      return this._runStreamLegacy(task);
    }

    // ── NEW CLI: streaming ACP turn ─────────────────────────────────────────
    // ACP is a persistent process — the model is fixed at spawn time. If the
    // caller asks for a DIFFERENT model than the one this process was spawned
    // with, setModel() below (called from the IPC handler ahead of run(), or
    // inline here as a fallback) triggers a respawn; queue this run behind it.
    if (model && model !== this._currentModel) {
      this.setModel(model);
      // setModel() (ACP path) restarts the process asynchronously; once ready
      // again it will accept new turns normally. Tell the caller to retry —
      // mirrors the existing "session is busy" pattern so the renderer doesn't
      // need special-case handling for a respawn-in-progress.
      this.emit(this.id, { type: "notice", level: "warn", msg: "Switching model — resend your message in a moment." });
      return false;
    }

    if (!this.proc) return false;
    this.busy = true;
    this.lastUsedAt = Date.now();
    this._deltasSinceAssistant = false;
    this._streamBuffer = "";
    this._streamChars = 0;
    this._lastProgressAt = 0;
    this._loopGuard = { lastSig: null, sameCount: 0, totalCalls: 0 };
    this._loopTripped = false;

    const content = this._buildTurnContent(task);
    this._currentTurnId = randomUUID();
    this._writeRpc(this._currentTurnId, "session/prompt", {
      sessionId: this.sessionId,
      prompt: [{ type: "text", text: content }],
    });
    return true;
  }

  respondConfirm(requestId, answer) {
    // Fetch+remove the pending permission ONCE (used by every protocol branch
    // below — do NOT re-fetch, it's already deleted after this).
    const perm = this._pendingPerms.get(requestId);
    this._pendingPerms.delete(requestId);
    const allowed = answer !== "no";

    // "Always allow": remember this tool for the rest of the session so it won't
    // prompt again. This is an explicit user choice — the only auto-approve path.
    // Also persist the decision to the gateway DB so future sessions auto-allow
    // the same tool without prompting (ainxt.user_connector_permissions).
    if (answer === "always" && perm && perm.tool) {
      this._alwaysAllowTools.add(perm.tool);
      this._persistAlwaysAllow(perm.tool);
    }

    // OLD CLI (streamjson): reply to the CLI's can_use_tool control_request over
    // the persistent stdin with a control_response. Preserve the tool's ORIGINAL
    // input — an empty updatedInput can strip args for MCP tools (e.g.
    // build_document loses its code). "Always allow" adds a session-scoped rule.
    if (this._protocol === "streamjson") {
      let response;
      if (answer === "no") {
        response = { behavior: "deny", message: "User denied this action." };
      } else {
        response = { behavior: "allow", updatedInput: (perm && perm.input) || {} };
        if (answer === "always" && perm && perm.tool) {
          response.updatedPermissions = [{
            type: "addRules",
            rules: [{ toolName: perm.tool }],
            behavior: "allow",
            destination: "session",
          }];
        }
      }
      this._writeStream({ type: "control_response", response: { subtype: "success", request_id: requestId, response } });
      return;
    }

    // ── NEW CLI (ACP) ──────────────────────────────────────────────────────
    if (perm && perm.isPermissionRequest) {
      // session/request_permission response format (confirmed from binary analysis):
      //   RequestPermissionResponse { outcome: RequestPermissionOutcome }
      //   RequestPermissionOutcome is internally-tagged on field "outcome":
      //     Selected  -> { outcome: "selected", optionId: <string> }   (SelectedPermissionOutcome)
      //     Cancelled -> { outcome: "cancelled" }
      // Pick the matching option from the options array the CLI sent — echo its exact optionId.
      // "Always allow" → pick the CLI's allow_always option so IT also remembers
      // the grant for the session (falls back to allow_once if not offered).
      // Use _writeLine directly to preserve the original integer id (id: 0).
      const options = perm.options || [];
      let chosen;
      if (!allowed) {
        chosen = options.find((o) => o.kind === "reject_once")
          || options.find((o) => o.optionId === "reject-once")
          || { optionId: "reject-once" };
      } else if (answer === "always") {
        chosen = options.find((o) => o.kind === "allow_always")
          || options.find((o) => o.kind === "allow_once")
          || options.find((o) => o.optionId === "allow-always")
          || { optionId: "allow-always" };
      } else {
        chosen = options.find((o) => o.kind === "allow_once")
          || options.find((o) => o.optionId === "allow-once")
          || { optionId: "allow-once" };
      }
      const result = { outcome: { outcome: "selected", optionId: chosen.optionId } };
      this._writeLine({ jsonrpc: "2.0", id: requestId, result });
    } else {
      // Legacy agent/confirm: { allowed: bool }
      this._writeRpcResponse(requestId, { allowed, reason: allowed ? null : "User denied." });
    }
  }

  interrupt() {
    if (this._protocol === "streamjson") {
      // OLD CLI: kill the persistent `--full` stream-json process, if any. Since
      // proc becomes null after, run()'s `if(!this.proc) return false` guards a
      // dead session (no lazy re-spawn yet — just ensure Stop works cleanly).
      this._interrupted = true; // close() will emit a clean "interrupted" result
      const proc = this.proc;
      const pid = proc && proc.pid;
      if (proc) {
        try {
          // On Windows a plain proc.kill() leaves the CLI's child processes
          // running (and stdout never closes), so the turn never ends. taskkill
          // /T /F reaps the whole tree. On POSIX proc.kill() is enough.
          if (process.platform === "win32" && pid) {
            spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true });
          } else {
            proc.kill("SIGKILL");
          }
        } catch { /* ignore */ }
      }
      this.proc = null;
      this.busy = false;
      // Safety net: if no live process was killable (proc already gone, or the
      // close event is delayed), still clear the UI busy state now. The flag on
      // close() prevents a duplicate result if the kill's close DOES fire.
      if (!proc) {
        this._interrupted = false;
        this.emit(this.id, {
          type: "result", status: "interrupted", response: "", error: undefined,
          model: null, elapsedMs: 0, costUsd: 0, costTotalUsd: this._lastTotalCost,
          usage: { input: 0, output: 0 }, numTurns: 0,
        });
      }
      return;
    }
    // NEW CLI (ACP): cancel the in-flight turn GRACEFULLY. The agent method is
    // `session/cancel` (a client→agent notification carrying sessionId),
    // confirmed from the CLI binary's agent method table
    // (session/new · load · set_mode · prompt · cancel · set_model).
    //
    // The previous code sent `notifications/cancelled` with a requestId — a
    // method the ACP agent does not handle — so Stop never took effect AND the
    // UI stayed "busy" forever (no result was emitted). Two fixes:
    //   1) Send the correct `session/cancel` notification.
    //   2) Immediately clear busy + emit an "interrupted" result so the Stop
    //      button releases right away.
    // We deliberately DO NOT kill the process here (unlike streamjson): the ACP
    // agent is a persistent JSON-RPC server reused for the NEXT prompt, and
    // run()'s acp branch has no re-spawn (`if (!this.proc) return false`).
    // Killing it would wedge every future turn. The agent stops the turn on
    // session/cancel and stays ready. A late prompt_complete/result for the
    // cancelled turn is ignored because busy is already false.
    this._interrupted = true;
    if (this.sessionId) {
      this._writeRpcNotification("session/cancel", { sessionId: this.sessionId });
    }
    this.busy = false;
    this._currentTurnId = null;
    this.emit(this.id, {
      type: "result", status: "interrupted", response: "", error: undefined,
      model: null, elapsedMs: 0, costUsd: 0, costTotalUsd: this._lastTotalCost,
      usage: { input: 0, output: 0 }, numTurns: 0,
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

  // ACP handshake: initialize → authenticate → session/new. Only the ACP path
  // has a persistent process to handshake with; the OLD single-shot CLI has no
  // handshake (each `--json` turn is self-contained), so _initialize is a no-op
  // there and is never invoked for it.
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

      // 3. session/new — mcpServers must be empty array (ACP v0.2.101 does not
      // support the sse+headers MCP format via session/new params). The office
      // persona + connector context is injected into the first user message by
      // run() via _contextInjected / _systemPrompt(). Gateway MCP connectors
      // are reached through the gateway API directly, not via local MCP wiring.
      const sessionNewId = randomUUID();
      this._writeRpc(sessionNewId, "session/new", {
        cwd: this.cwd || process.cwd(),
        mcpServers: [],
      });
      const sessionResult = await new Promise((resolve, reject) => {
        this._pendingCtrl.set(sessionNewId, { resolve, reject });
        setTimeout(() => { this._pendingCtrl.delete(sessionNewId); reject(new Error("session/new timed out")); }, 15000);
      });

      const meta = initResult._meta || {};
      const sessionId = (sessionResult && sessionResult.sessionId) || meta.agentInstanceId || null;
      if (sessionId) { this.sessionId = sessionId; this.emit(this.id, { type: "session:id", sessionId }); }
      const cmds = Array.isArray(meta.availableCommands) ? meta.availableCommands : [];
      this.emit(this.id, {
        type: "session:init",
        model: (initResult.modelState && initResult.modelState.currentModelId) || null,
        permissionMode: this._permMode || "default",
        slashCommands: cmds.map((c) => ({ name: c.name, description: c.description || "", argumentHint: (c.input && c.input.hint) || "" })),
        tools: [], skills: [],
      });
    } catch (e) {
      // Re-throw so the spawn handler's .catch() can emit the error and
      // resolve _readyResolvers(false) — session doesn't hang.
      throw e;
    }
  }

  // Register Buddy MCP servers using the CLI's own `ainxt mcp add` command.
  // This is the only reliable way — manual TOML editing is fragile.
  // The CLI writes the correct format and handles duplicates safely.
  _injectMcpIntoConfig() {
    if (!this.gatewayBase || !this.jwt) return;
    const { execFileSync } = require("child_process");
    const bin = resolveCliBinary();
    if (!bin) return;

    const base = String(this.gatewayBase).replace(/\/+$/, "");
    const added = [];
    // Note: ainxt mcp add overwrites existing entries — no need to remove first.
    // Removing before adding caused a race condition when two sessions started
    // simultaneously: session B's remove wiped session A's config mid-initialize.

    try {
      // Gateway connector MCP (Outlook, Teams, KB, docs, etc.)
      // CLI v0.2.101 uses streamable HTTP (POST) transport — MCP spec 2024-11-05.
      // The gateway POST /buddy/mcp/sse endpoint handles this transport.
      // For MCP connections the CLI uses rustls which rejects IP-based URLs
      // when the TLS cert CN is a hostname (NotValidForName error).
      // Replace any bare IP with the configured gateway hostname so TLS validation passes.
      // The hostname resolves to the same IP via DNS — no server change needed.
      const _gwHostname = new URL(String(this.gatewayBase)).hostname;
      const mcpUrl = `${base}${BUDDY_MCP_PATH}`
        .replace(/^https:\/\/[\d.]+/, `https://${_gwHostname}`);
      const buddyArgs = [
        "mcp", "add",
        "--transport", "http",
        "--header", `Authorization: Bearer ${this.jwt}`,
        "--scope", "user",
        "ainxt_buddy",
        mcpUrl,
      ];
      const allow = this.role && Array.isArray(this.role.allowed_connectors) ? this.role.allowed_connectors : [];
      if (allow.length) buddyArgs.push("--header", `x-buddy-allowed-tools: ${allow.join(",")}`);

      execFileSync(bin.command, [...bin.args, ...buddyArgs], {
        env: { ...process.env, ...TLS_ENV },
        timeout: 5000,
        stdio: "pipe",
      });
      added.push("ainxt_buddy");
      _trace("MCP_INJECT", `Registered ainxt_buddy via ainxt mcp add (url: ${mcpUrl})`);
    } catch (err) {
      // "already exists" = idempotent success (the server is registered), not an
      // outage. Only a real failure means zero connector tools.
      if (_mcpAlreadyExists(err)) {
        added.push("ainxt_buddy");
        _trace("MCP_INJECT", "ainxt_buddy already registered — reusing existing entry");
      } else {
        _trace("MCP_INJECT_ERROR", `ainxt_buddy: ${err.message}`);
        // LOUD FAILURE (was silent): if this registration fails the session still
        // starts, but with ZERO connector tools — no Outlook, no Teams, no
        // GitLab/Jira. The agent then improvises with whatever is left (its built-in
        // Bash/Read), which is exactly the "Buddy went to the command line instead of
        // GitLab" symptom. Tell the user instead of failing invisibly.
        this._connectorsUnavailable = true;
        this.emit(this.id, {
          type: "notice",
          level: "warn",
          msg: "Couldn't reach your connectors this session — GitLab, Jira, Outlook and "
             + "Teams are unavailable. Restart Buddy; if it persists, sign out and back in.",
        });
      }
    }

    try {
      // Desktop local MCP (browser/computer-use tools)
      if (this.localMcpPort) {
        const root = this._grantedRoot ? `&root=${encodeURIComponent(this._grantedRoot)}` : "";
        execFileSync(bin.command, [
          ...bin.args,
          "mcp", "add",
          "--transport", "http",
          "--scope", "user",
          "ainxt_desktop",
          `http://127.0.0.1:${this.localMcpPort}/sse?surface=buddy${root}`,
        ], { env: { ...process.env }, timeout: 5000, stdio: "pipe" });
        added.push("ainxt_desktop");
        _trace("MCP_INJECT", "Registered ainxt_desktop via ainxt mcp add");
      }
    } catch (err) {
      if (_mcpAlreadyExists(err)) {
        added.push("ainxt_desktop");
        _trace("MCP_INJECT", "ainxt_desktop already registered — reusing existing entry");
      } else {
        _trace("MCP_INJECT_ERROR", `ainxt_desktop: ${err.message}`);
        // LOUD FAILURE: without ainxt_desktop the `upload_file_to_chat` tool is gone,
        // so Buddy cannot attach local folder files — tell the user instead of
        // letting the agent improvise an apology mid-send.
        this._desktopToolsUnavailable = true;
        this.emit(this.id, {
          type: "notice", level: "warn",
          msg: "Local file upload is unavailable this session — Buddy can't attach files "
             + "from your folder. Restart Buddy; if it persists, sign out and back in.",
        });
      }
    }

    this._injectedMcpServers = added;
  }

  // Remove the Buddy MCP servers using `ainxt mcp remove`.
  _removeMcpFromConfig() {
    const { execFileSync } = require("child_process");
    const bin = resolveCliBinary();
    if (!bin) return;

    const toRemove = (this._injectedMcpServers && this._injectedMcpServers.length)
      ? this._injectedMcpServers
      : ["ainxt_buddy", "ainxt_desktop"]; // always clean both on startup
    // Also sweep the legacy "ainxt_buddy" name for users upgrading from a
    // previous build — otherwise their config.toml keeps a stale entry and the
    // CLI advertises every Buddy tool twice (once under each prefix). The
    // `ainxt mcp remove` call is tolerant of "not present", so this is safe
    // to run unconditionally.
    if (!toRemove.includes("ainxt_buddy")) toRemove.push("ainxt_buddy");

    for (const name of toRemove) {
      try {
        execFileSync(bin.command, [...bin.args, "mcp", "remove", name], {
          env: { ...process.env },
          timeout: 5000,
          stdio: "pipe",
        });
        _trace("MCP_REMOVE", `Removed ${name} via ainxt mcp remove`);
      } catch {
        // Server may not exist — that's fine, ignore the error
      }
    }
    this._injectedMcpServers = [];
  }

  // Shared, TOML-table-aware writer for [models.extra_headers] in config.toml.
  //
  // Root cause of the bug this replaces: _injectSurfaceHeader() and
  // _injectConvIdHeader() each used regex string-surgery that only recognised
  // the inline-map form `extra_headers = { ... }`. The ainxt CLI itself always
  // writes headers as a dotted-table section:
  //   [models.extra_headers]
  //   x-ainxt-surface = "buddy"
  // Neither old function recognised that form, so whichever ran second fell
  // through to its "nothing exists" branch and appended a SECOND `[models]`
  // block with a conflicting inline `extra_headers = {...}`. TOML forbids
  // defining the same table key twice, so the CLI failed to parse config.toml
  // on the very next spawn with "duplicate key `extra_headers`" — breaking
  // every Buddy turn (reads, sends, attachments) until the file was repaired.
  //
  // This helper always writes/updates the dotted-table form the CLI uses, and
  // recognises both that form and the legacy inline-map form as "already
  // present", so repeated calls can never produce two conflicting definitions.
  _injectExtraHeader(key, value) {
    const os = require("os");
    const path = require("path");
    const fs = require("fs");
    const home = process.env.AINXT_HOME || path.join(os.homedir(), ".ainxt");
    const configPath = path.join(home, "config.toml");
    let content = "";
    try { content = fs.readFileSync(configPath, "utf-8"); } catch { /* new file */ }

    const escKey  = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const KEY_RE  = new RegExp(`^"?${escKey}"?\\s*=\\s*"[^"]*"\\s*$`, "m");
    const NEW_LINE = `"${key}" = "${value}"`;
    const TABLE_HEADER_RE = /^\[models\.extra_headers\]\s*$/m;

    if (TABLE_HEADER_RE.test(content)) {
      // Dotted-table form already exists — update the key in place if present,
      // else insert right after the last non-blank line of that section.
      const lines = content.split("\n");
      const headerIdx = lines.findIndex((l) => TABLE_HEADER_RE.test(l));
      // Find the next `[section]` header after ours — that ends our section.
      // Array methods (not a for/while loop) over the fixed-length `lines`
      // array, so there is no loop condition for tainted input to drive.
      const nextHeaderOffset = lines.slice(headerIdx + 1).findIndex((l) => /^\[/.test(l));
      const endIdx = nextHeaderOffset === -1 ? lines.length : headerIdx + 1 + nextHeaderOffset;
      const existingIdx = lines.slice(headerIdx + 1, endIdx).findIndex((l) => KEY_RE.test(l));
      if (existingIdx >= 0) {
        const lineIdx = headerIdx + 1 + existingIdx;
        if (lines[lineIdx] === NEW_LINE) return; // already correct — no write needed
        lines[lineIdx] = NEW_LINE;
      } else {
        // Insert right after the last non-blank line of the section — walk
        // backward from the end via findIndex on a reversed slice.
        const trailingBlankCount = lines.slice(headerIdx + 1, endIdx).slice().reverse()
          .findIndex((l) => l.trim() !== "");
        const insertAt = trailingBlankCount === -1 ? headerIdx + 1 : endIdx - trailingBlankCount;
        lines.splice(insertAt, 0, NEW_LINE);
      }
      content = lines.join("\n");
    } else if (/^extra_headers\s*=\s*\{/m.test(content)) {
      // Legacy inline-map form — update the key in place or append inside { }.
      const INLINE_KEY_RE = new RegExp(`"?${escKey}"?\\s*=\\s*"[^"]*"`);
      if (content.includes(NEW_LINE)) {
        return; // already correct — no write needed
      } else if (INLINE_KEY_RE.test(content)) {
        content = content.replace(INLINE_KEY_RE, NEW_LINE);
      } else {
        content = content.replace(
          /^(extra_headers\s*=\s*\{)([^}]*)(\})/m,
          (_, open, inner, close) => {
            const trimmed = inner.trimEnd();
            const sep = trimmed && !trimmed.endsWith(",") ? ", " : "";
            return `${open}${trimmed}${sep}${NEW_LINE} ${close}`;
          }
        );
      }
    } else {
      // Neither form exists yet — append a new dotted-table section matching
      // the CLI's own on-disk format so future writes recognise it.
      content += `${content && !content.endsWith("\n") ? "\n" : ""}\n[models.extra_headers]\n${NEW_LINE}\n`;
    }

    try { fs.mkdirSync(home, { recursive: true }); } catch { /* already exists */ }
    fs.writeFileSync(configPath, content, "utf-8");
  }

  // Write x-ainxt-surface: buddy into [models.extra_headers] in config.toml
  // so every inference request the Buddy CLI makes carries this header.
  // The gateway reads it to tag model_usages.source_channel as DESKTOP-BUDDY
  // instead of CLI (the CLI binary always sends X-AiNxt-Client: cli/*, so
  // without this header the gateway cannot distinguish Buddy from Code tab).
  _injectSurfaceHeader() {
    try {
      this._injectExtraHeader("x-ainxt-surface", "buddy");
      _trace("SURFACE_HEADER_INJECT", "x-ainxt-surface: buddy ensured in config.toml");
    } catch (err) {
      _trace("SURFACE_HEADER_INJECT_ERROR", err.message);
      // Non-fatal: session still starts, source_channel will show as CLI
    }
  }

  // Write x-ainxt-conv-id: <convId> into [models.extra_headers] in config.toml
  // so the gateway's Redis-backed Buddy history pipeline can key on the durable
  // conversation id. Called on every run() so it always reflects the CURRENT
  // conversation (unlike x-ainxt-surface which is static per-session).
  //
  // The gateway reads this header in messages_compat_router.py and uses it to:
  //   1. Load prior Redis history for this conversation (resume-failure recovery)
  //   2. Save user+assistant turns to Redis after each response
  // Without this header, conv_id is "" and the entire Redis pipeline is skipped —
  // meaning history is only as durable as the Postgres save from persistCurrent(),
  // which misses in-flight turns when the app closes mid-response.
  _injectConvIdHeader(convId) {
    try {
      this._injectExtraHeader("x-ainxt-conv-id", convId);
      _trace("CONV_ID_HEADER_INJECT", { convId });
    } catch (err) {
      _trace("CONV_ID_HEADER_INJECT_ERROR", err.message);
      // Non-fatal — session continues; Redis pipeline falls back to Postgres-only
    }
  }

  async setModel(model) {
    const next = model || this._currentModel;
    if (!next) return true;

    // ── streamjson (single-shot, production default) ───────────────────────
    // No persistent process — just remember it. The very next run() call
    // passes it straight to _runLegacy()'s --model flag, taking effect
    // immediately with no restart.
    if (this._protocol === "streamjson") {
      this._currentModel = next;
      return true;
    }

    // ── ACP (persistent process, SIT/testing only) ──────────────────────────
    // No live model-switch RPC exists in this CLI version — --model is
    // spawn-time only, so switching models means tearing the process down and
    // spawning a fresh one with the new --model, resuming the same agent
    // session (--resume, wired in _spawnArgsAcp via this.sessionId) so
    // conversation continuity survives the restart.
    if (next === this._currentModel) return true; // no-op — already this model

    if (this.busy) {
      // Never interrupt an in-flight turn/tool call for a model switch — defer
      // it. The turn_complete handler below drains this once the turn ends.
      this._pendingModelSwitch = next;
      this.emit(this.id, { type: "notice", level: "info", msg: "Model switch queued — will apply after the current turn finishes." });
      return true;
    }

    this._currentModel = next;
    return this._respawnAcp();
  }

  // Tear down the current ACP process and spawn a new one with the (already
  // updated) this._currentModel, preserving the conversation via --resume.
  // Only called when !this.busy (see setModel()), so no in-flight turn is
  // ever interrupted by this.
  async _respawnAcp() {
    this.emit(this.id, { type: "notice", level: "info", msg: "Switching model…" });
    this.ready = false;
    const oldProc = this.proc;
    if (oldProc) {
      // Set BEFORE killing so the existing proc.on("close", ...) handler (added
      // back in _spawn()) sees _respawning=true and skips session:exit/cleanup.
      this._respawning = true;
      const pid = this.pid;
      try { oldProc.stdin.end(); } catch { /* ignore */ }
      const _killGroup = (sig) => {
        try {
          if (pid && process.platform !== "win32") process.kill(-pid, sig);
          else oldProc.kill(sig);
        } catch { /* already gone */ }
      };
      // Wait for the OLD process to actually finish exiting before spawning its
      // replacement and flipping _respawning back off — NOT a fixed timer. A
      // fixed timer risks the old process's (delayed) "close" event firing
      // AFTER _respawning has already reset to false, which would wrongly emit
      // session:exit / tear down MCP config for what is really just a restart,
      // and would race with the new process's own proc/pid bookkeeping.
      await new Promise((resolve) => {
        let settled = false;
        const done = () => { if (!settled) { settled = true; resolve(); } };
        oldProc.once("close", done);
        oldProc.once("exit", done);
        _killGroup("SIGTERM");
        setTimeout(() => _killGroup("SIGKILL"), 1500);
        setTimeout(done, 3000); // safety net — never hang a model switch forever
      });
      try { require("./pidRegistry").remove(pid); } catch { /* optional */ }
      this._respawning = false;
    }
    // Reset cost tracking for the new process — sessionCostTicks restarts
    // from 0 on every new CLI spawn, so _lastTotalCost must also reset.
    // Without this, the first turn after a model switch always shows $0.00
    // because sessionCostUsd (small, new process) < _lastTotalCost (large,
    // accumulated from old process) → turnCost = max(0, negative) = 0.
    this._lastTotalCost = 0;
    this._lastSessionCostTicks = 0;
    this.proc = null;
    try {
      const ok = await this._spawn();
      if (ok) this.emit(this.id, { type: "notice", level: "info", msg: `Now using ${this._currentModel}.` });
      return ok;
    } catch (e) {
      this.emit(this.id, { type: "error", msg: `Couldn't switch models: ${e.message}` });
      return false;
    }
  }

  async setPermissionMode(mode) {
    // Store locally for use in the file-read boundary and destructive tool checks.
    this._permMode = mode || "default";
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
      _trace("STDIN_BLOCKED", { reason: !this.proc ? "no proc" : "stdin not writable" });
    }
  }

  // Fire-and-forget usage report → gateway (enterprise analytics + group spend).
  _reportUsage(costUsd, usage) {
    if (!this.gatewayBase || !this.jwt) return;
    try {
      const base = String(this.gatewayBase).replace(/\/+$/, "");
      const u = new URL(`${base}/ainxt/v1/api/buddy/usage`);
      const lib = u.protocol === "https:" ? require("https") : require("http");
      const payload = JSON.stringify({
        cost_usd: costUsd || 0, surface: "buddy", model: "claude",
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
    // Remove x-ainxt-conv-id from config.toml so the next session does not
    // inherit a stale conversation ID and loop back to old history.
    // x-ainxt-surface is left in place — it is static and correct for all sessions.
    try { this._removeExtraHeader("x-ainxt-conv-id"); } catch { /* non-fatal */ }
  }

  // Remove a key from [models.extra_headers] in config.toml entirely.
  // Used on session cleanup to prevent stale conv-id from persisting.
  _removeExtraHeader(key) {
    const os = require("os");
    const path = require("path");
    const fs = require("fs");
    const home = process.env.AINXT_HOME || path.join(os.homedir(), ".ainxt");
    const configPath = path.join(home, "config.toml");
    let content = "";
    try { content = fs.readFileSync(configPath, "utf-8"); } catch { return; /* nothing to remove */ }

    const escKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const KEY_RE = new RegExp(`^"?${escKey}"?\\s*=\\s*"[^"]*"\\s*\n?`, "m");
    if (!KEY_RE.test(content)) return; // key not present — nothing to do

    content = content.replace(KEY_RE, "");
    try { fs.writeFileSync(configPath, content, "utf-8"); } catch { /* non-fatal */ }
    _trace("CONV_ID_HEADER_REMOVE", { key });
  }

  dispose() {
    this._cleanupMcpConfig();
    // OLD streamjson now runs a PERSISTENT `--full` process — kill the whole tree
    // (mirror the ACP dispose kill below), then emit session:exit ONCE (guarded)
    // so the UI tears the session down cleanly even if the close event is delayed.
    if (this._protocol === "streamjson") {
      const proc = this.proc;
      const pid = this.pid;
      if (proc) {
        try { proc.stdin && proc.stdin.end(); } catch { /* ignore */ }
        try {
          // On Windows a plain proc.kill() leaves the CLI's children running;
          // taskkill /T /F reaps the whole tree. On POSIX we spawned detached, so
          // kill(-pid) reaps the CLI + its children.
          if (process.platform === "win32" && pid) {
            spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true });
          } else if (pid) {
            process.kill(-pid, "SIGKILL");
          } else {
            proc.kill("SIGKILL");
          }
        } catch { /* already gone */ }
        try { require("./pidRegistry").remove(pid); } catch { /* optional */ }
        this.proc = null;
      }
      if (!this._exitEmitted) {
        this._exitEmitted = true;
        this.ready = false;
        this.emit(this.id, { type: "session:exit", code: 0 });
      }
      return;
    }
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
// The CLI is single-session (one process = one conversation, serial
// turns), so N conversations need N processes — but we CAP how many stay resident
// and reap idle ones. Because every conversation persists its agent session_id
// (buddy_conversations.resume_id) and reopening uses --resume, evicting an idle
// session is LOSSLESS: returning to it rehydrates the full context on demand.
// Result: "many open chats" → "≤ MAX live processes + resume-on-demand".
const MAX_BUDDY_SESSIONS = Math.max(1, parseInt(process.env.MAX_BUDDY_SESSIONS || "4", 10));
// Hard ceiling for the busy-overflow case (all sessions running): we allow SOME
// overflow so a burst of concurrent tasks isn't rejected, but never unbounded — past
// this, creation is refused so the user's machine can't be flooded with agent
// processes. (G20)
const MAX_BUDDY_HARD_CAP = Math.max(MAX_BUDDY_SESSIONS,
                            parseInt(process.env.MAX_BUDDY_HARD_CAP || String(MAX_BUDDY_SESSIONS * 2), 10));
const IDLE_TIMEOUT_MS     = Math.max(60_000, parseInt(process.env.BUDDY_IDLE_TIMEOUT_MIN || "15", 10) * 60_000);
const REAP_INTERVAL_MS    = 60_000;

class BuddySessionManager {
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
    if (this.sessions.size >= MAX_BUDDY_HARD_CAP) {
      return { error: "too_many_sessions",
               message: `Too many tasks are running at once (${this.sessions.size}). `
                      + "Wait for one to finish or stop it, then try again." };
    }
    const id = `co${++this._seq}_${Date.now().toString(36)}`;
    const session = new BuddyOfficeSession(id, cwd, this.emit, opts || {});
    this.sessions.set(id, session);
    const ok = await session.start();
    return { id, ready: ok, cwd, resumeId: (opts && opts.resumeId) || null };
  }

  // Evict the least-recently-used NON-busy session when at capacity. A busy session
  // (a running task) is never evicted; if all are busy we allow a temporary overflow
  // rather than kill in-flight work — the reaper trims it once one goes idle.
  _evictIfFull() {
    while (this.sessions.size >= MAX_BUDDY_SESSIONS) {
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
  run(id, payload) { const s = this.get(id); return s ? s.run(payload) : false; }
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

module.exports = { BuddySessionManager };
