// SPDX-License-Identifier: MIT
/* CoworkDesktop — desktop "AI office employee" mode (the office-flavored sibling
 * of Code.jsx).
 *
 * Runs the FULL ainxt office agent ON THE USER'S MACHINE via the local CLI
 * (spawned by the desktop main process, driven over the NDJSON --stream-json
 * protocol). Unlike Code (which opens a local repo and edits source files), this
 * is the office persona: it reads documents, drafts content, builds Word / Excel
 * / PowerPoint, and works through connectors (Outlook/Teams, Jira, Confluence) —
 * all locally, with a multi-turn conversation.
 *
 * It drives a SEPARATE desktop IPC namespace, `window.ainxtDesktop.coworkOffice.*`
 * (helpers in hooks/useDesktop.js mirror the `cowork*` ones — `coworkOffice*`).
 * That keeps the office session pool, history store, and persona distinct from
 * the engineer "Code" session, so the two tabs never cross-talk.
 *
 * A local folder/working-dir is OPTIONAL here: office work happens through
 * connectors and documents, not a checked-out repo. If a folder IS chosen the
 * agent may read/write files there; otherwise it works purely from connectors +
 * attached documents.
 *
 * AiNxt guardrails (handled by the CLI/gateway, surfaced here in the UI):
 *  - Compliance REDACTS reads (it never blocks the user) but HARD-BLOCKS sensitive
 *    content on outbound writes/sends — a blocked send arrives as an `error`
 *    notice block, not a silent drop.
 *  - Connector/document WRITES never auto-execute: they pause on a `confirm`
 *    permission prompt (the PermissionBar) and only run after explicit approval.
 *    Never log secrets/tokens anywhere in this file.
 *
 * Desktop-only: gated behind `isCoworkOfficeAvailable`. In a browser the parent
 * (Office.jsx) renders the server-side office UI instead; this component shows an
 * explanatory placeholder if mounted there directly.
 */
import { useEffect, useReducer, useRef, useState, useCallback, useMemo } from "react";
import {
  FolderOpen, SendHorizontal, CirclePauseIcon, Terminal, FileDiff, Loader2,
  ShieldQuestion, CheckCircle2, XCircle, Briefcase, Cpu, MonitorSmartphone, Plus,
  MessageSquare, Trash2, Gauge, ShieldCheck, Pencil, Map as MapIcon,
  Copy, Check, Volume2, VolumeX, RotateCcw, Plug, FileText, FileSpreadsheet, X, Clock,
  ArrowDown, Brain, Paperclip, Download,
} from "lucide-react";
import {
  isCoworkOfficeAvailable, coworkOfficeAuthState, coworkOfficeLogin, coworkOfficeCancelLogin, coworkOfficeOnLoginOutput,
  coworkOfficeOnFlushBeforeQuit, coworkOfficeFlushDone,
  coworkOfficeCreateSession, coworkOfficeRun, coworkOfficeRespondConfirm, coworkOfficeInterrupt,
  coworkOfficeOnEvent, coworkOfficeOnAuthUpdated, pickFolder, pickFile, listFolder, readFile, readFileBinary, readFileSpreadsheet,
  coworkOfficeSetModel, coworkOfficeSetPermissionMode, coworkOfficeAdoptToken,
  coworkOfficeHasValidKey, getMcpPort, openExternal,
} from "../hooks/useDesktop.js";
import { API_BASE, authFetch, MODEL_PICKER, MODEL_DEFAULT, MODEL_DEFAULT_LOCKED, MODEL_ALIASES } from "../config";
import { extractDocxHtml } from "../utils/docxTextExtractor.js";
import { parseXlsx } from "../utils/xlsxParser.js";
import { extractPptxHtml } from "../utils/pptxTextExtractor.js";
import MessageMeta from "./MessageMeta.jsx";
import BrandMark from "./BrandMark.jsx";
import CoworkScheduler from "./CoworkScheduler.jsx";
import { useConfirm, useToast } from "./ui/DialogProvider";
import { usePromptQueue } from "../hooks/usePromptQueue.js";

/* The effects below sync SERVER-persisted conversation history and the live CLI
 * session id into React state on workspace/folder/auth changes —
 * an intentional "external system → React" sync (the exact pattern Code.jsx ships).
 * react-hooks v7's set-state-in-effect reachability analysis flags these because
 * the office variant drops the "folder required" guards (office work needs no
 * folder); the syncs are correct, so the rule is disabled for this file only. */
/* eslint-disable react-hooks/set-state-in-effect */

// Conversation DATA stays server-persisted (Postgres). The ONE exception is this
// lightweight UI cursor — the id of the last-open conversation — so a remount /
// reload / long tab-away can restore the user's place instead of a blank new chat.
// (App.jsx already uses localStorage for nav_collapsed; this mirrors that.)
const LAST_CONV_KEY = "buddy_last_conv";

// NO localStorage for conversation DATA. ALL Buddy state is server-persisted
// (Postgres) via the gateway: conversations → /buddy/conversations, projects →
// /buddy/projects, schedules → /buddy/tasks, memory → /buddy/memory.
function _folderName(folder) {
  return folder ? (folder.split("/").filter(Boolean).pop() || folder) : "Office";
}
// Conversation history is SERVER-persisted (Postgres /buddy/conversations) — NO
// localStorage. Scoped to the JWT user, optionally project-linked.
async function convList() {
  try {
    const r = await authFetch(`${API_BASE}/buddy/conversations`);
    const d = await r.json();
    return (d?.conversations || []).map((c) => ({
      id: c.id, folder: c.folder || null, folderName: _folderName(c.folder),
      projectId: c.project_id || "", title: c.title || "Conversation",
      resumeId: c.resume_id || null,
      createdAt: c.created_at ? Date.parse(c.created_at) : 0,
      updatedAt: c.updated_at ? Date.parse(c.updated_at) : 0,
      // Sort by CREATION time (stable) — newest task on top, and the order never
      // shuffles just because you opened/persisted a task (which bumps updated_at).
    })).sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
  } catch { return []; }
}
async function convGetFull(id) {
  try { const r = await authFetch(`${API_BASE}/buddy/conversations/${id}`); return r.ok ? await r.json() : null; }
  catch { return null; }
}
// Strip transient streaming flags and empty trailing assistant messages so a
// conversation saved mid-stream doesn't reopen stuck on "Thinking…" or with
// a blank assistant bubble at the end (happens when the app closes while a
// turn is in-flight — the assistant message exists but has no blocks yet).
function _sanitize(messages) {
  const cleaned = (messages || []).map((m) => (m.streaming ? { ...m, streaming: false } : m));
  // Drop a trailing empty assistant message (no text blocks, no tool blocks,
  // no doc/notice blocks) — it's an artefact of a mid-turn close and adds
  // nothing to the restored conversation. The Redis pipeline restores the
  // model's context independently of what the UI shows.
  while (cleaned.length > 0) {
    const last = cleaned[cleaned.length - 1];
    if (last.role === "assistant" && (!last.blocks || last.blocks.length === 0)) {
      cleaned.pop();
    } else {
      break;
    }
  }
  return cleaned;
}
// Upsert a conversation to the server (fire-and-forget — never blocks a turn).
function convSave(conv) {
  if (!conv?.id) return;
  authFetch(`${API_BASE}/buddy/conversations/${conv.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: conv.title || "Conversation",
      messages: _sanitize(conv.messages),
      project_id: conv.projectId || null,
      folder: conv.folder || null,
      // Persist the agent session id so the task can be --resumed after restart.
      resume_id: conv.resumeId || null,
    }),
  }).catch(() => {});
}
async function convDelete(id) {
  try { await authFetch(`${API_BASE}/buddy/conversations/${id}`, { method: "DELETE" }); } catch { /* ignore */ }
}
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import { mdComponents, DocDownloadButton, parseDocMarkers } from "./Message.jsx";

// Real model ids the gateway routes for the headless office agent.
// FALLBACK ONLY — used until GET /all-models resolves (offline/slow first paint),
// so the picker never renders empty. Once loaded, Buddy's picker is sourced from
// the SAME /all-models + /model-governance/my-models endpoints Chat.jsx uses (see
// the `models` derivation further down) — every provider (Claude, OpenAI,
// Google/Gemini, Local/in-house) the platform can route to, governance-filtered.
// From config.js (VITE_MODEL_PICKER) so a deployment serving its own models
// gets its own fallback picker instead of a list of ids it cannot route to.
const BASE_MODELS = MODEL_PICKER;
// Model lock is OPS-CONFIGURABLE via the gateway (GET /buddy/model-config, driven by
// BUDDY_FORCED_MODEL / BUDDY_MODEL_LOCKED env). These are only the fallbacks used
// until that config loads. The picker + /model command re-enable automatically when
// the config reports locked=false. Per-provider routing is UNCHANGED — lock only
// fixes which model Buddy selects.
const DEFAULT_LOCKED_MODEL = MODEL_DEFAULT_LOCKED;
const DEFAULT_MODEL_LOCKED = true;
// Backwards-compat alias so slash-command / label lookups still resolve base ids
// even before the dynamic list loads. The component-level `models` value (derived
// from /all-models, see below) is the authoritative, full-catalog list used for
// the picker.
const MODELS = BASE_MODELS;
const _localLabel = (id) => {
  // "local:llama3-70b" → "Llama3 70b (local)"
  const name = (id || "").replace(/^local:/, "");
  const pretty = name.replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  return `${pretty} (local)`;
};
const MODEL_LABEL_FROM = (list, id) =>
  list.find((m) => m.key === id)?.label
  || (id?.startsWith("local:") ? _localLabel(id) : (id || "").replace(/^claude-/, ""))
  || "model";
const MODEL_LABEL = (id) => MODEL_LABEL_FROM(BASE_MODELS, id);

// Permission modes the agent accepts via set_permission_mode. For office work the
// default ("Ask each time") is the safe one — connector sends + document writes
// must be confirmed; auto-accept only affects local file edits.
const PERM_MODES = [
  { key: "default",          label: "Ask each time", icon: ShieldCheck },
  { key: "acceptEdits",      label: "Auto-accept actions", icon: Pencil },
  { key: "plan",             label: "Plan mode", icon: MapIcon },
];
const PERM_LABEL = (k) => PERM_MODES.find((m) => m.key === k)?.label || "Ask each time";

function fmtCost(u) {
  if (u == null) return "";
  if (u === 0) return "$0";
  return u < 0.01 ? `$${u.toFixed(4)}` : `$${u.toFixed(2)}`;
}
function fmtTok(n) {
  if (!n) return "0";
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : `${n}`;
}

// Cosmetic-only: derive a cleaner conversation title from the raw first user
// message. Strips a leading request verb + filler ("summarize the attached…" →
// "attached…") so the sidebar reads like a topic, not a command. Purely local —
// no network. Fail-open: any empty result falls back to the original text.
function cleanConvTitle(text) {
  const raw = (text || "").trim();
  if (!raw) return "Conversation";
  const cleaned = raw
    // leading request verb + optional filler articles/preposition
    .replace(/^\s*(please\s+)?(summariz(e|es|ing)?|summarise|generate|create|convert|make|write|draft|build|extract|turn)\b/i, "")
    .replace(/^\s*(this|these|the|a|an|me|us|it|them|into|to)\b/i, "")
    .replace(/\s{2,}/g, " ")
    .trim();
  const out = (cleaned || raw).slice(0, 60).trim();
  if (!out) return "Conversation";
  return out.charAt(0).toUpperCase() + out.slice(1);
}

// ── Buddy REST fallback (CLI-missing) ─────────────────────────────────────────
// When the local cowork CLI binary isn't available, coworkSession._spawn() emits
// {type:"error"} + {type:"session:exit", code:-1}. For a DOCUMENT request we don't
// want to hard-fail — we route it through the SAME REST path Chat uses
// (POST /docs/generate) and surface the result via the existing `document`
// event → DocDownloadButton. Non-doc chat keeps the normal gating.

// Lightweight local doc-intent + format detector (no LLM hop — Buddy fallback is
// best-effort). Requires an ACTION context (verb / "give me a" / file extension)
// so passing mentions like "the pdf was confusing" don't trigger a doc build.
function detectDocRequest(text) {
  const t = (text || "").toLowerCase();
  const wantsDoc =
    /\b(word document|word doc|\.docx|\.pdf|\.xlsx|\.pptx|\.csv|\.md)\b/i.test(t)
    || /\b(generate|create|make|draft|build|write|give me|produce)\b.{0,30}\b(doc|document|file|report|spreadsheet|excel|workbook|powerpoint|presentation|slides?|deck|pdf|word)\b/i.test(t)
    || /\b(convert|turn|export)\b.{0,20}\b(this|it|that|into|to)\b/i.test(t)
    || /\bsummari[sz]e\b.*\b(document|doc|file|pdf|word|report)\b/i.test(t);
  if (!wantsDoc) return null;
  let format = "pdf";
  if (/\b(pptx|powerpoint|presentation|slides|deck)\b/.test(t)) format = "pptx";
  else if (/\b(csv|comma-separated|\.csv)\b/.test(t)) format = "csv";
  else if (/\b(xlsx|excel|spreadsheet|workbook|xls)\b/.test(t)) format = "xlsx";
  else if (/\b(word|docx|\.docx)\b/.test(t)) format = "docx";
  else if (/\bmarkdown|\.md\b/.test(t)) format = "md";
  else if (/\bplain text|\.txt\b/.test(t)) format = "txt";
  const intent = /\b(revise|edit|update|modify|change)\b/.test(t) ? "revise"
    : /\bconvert\b|turn (this|it|that) into|\binto (a )?(word|pdf|excel|powerpoint)\b/.test(t) ? "convert"
    : /\bsummari[sz]e\b/.test(t) ? "summarize"
    : /\bextract\b/.test(t) ? "extract"
    : "generate";
  return { format, intent };
}

// Empty-state starter prompts — office-flavored (connectors + documents), mirrors
// Office.jsx's SUGGESTIONS in tone.
const SUGGESTIONS = [
  { icon: FileText,        text: "Summarize the attached PDFs" },
  { icon: Plug,            text: "How many unread emails do I have in Outlook?" },
  { icon: FileSpreadsheet, text: "Turn this data into an Excel sheet with totals" },
  { icon: Briefcase,       text: "Draft a status update from my Jira board" },
];

// ── Conversation reducer ──────────────────────────────────────────────────────
// One entry per CLI session. Events from the office CLI mutate the latest
// assistant message's blocks; result finalizes the turn. Identical event protocol
// to Code.jsx — plus a `doc` block for generated-document download cards.

const initialState = { convs: {} };

function lastAssistant(conv) {
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    if (conv.messages[i].role === "assistant") return conv.messages[i];
  }
  return null;
}

function appendText(blocks, text) {
  const last = blocks[blocks.length - 1];
  if (last && last.kind === "text") last.text += text;
  else blocks.push({ kind: "text", text });
}

function applyEvent(conv, ev) {
  const a = lastAssistant(conv);
  switch (ev.type) {
    case "token":   if (a) appendText(a.blocks, ev.text); break;
    case "newline": if (a) appendText(a.blocks, "\n"); break;
    // 'line' is the history-only mirror of streamed tokens — ignored for display
    // (we reconcile to result.response at the end).
    case "line": break;
    case "tool:start":
      if (a) a.blocks.push({ kind: "tool", name: ev.name, detail: ev.detail, status: "running", diff: ev.diff || null });
      break;
    // A tool's (possibly long) input is being generated — show what's happening
    // instead of a lingering "Thinking…". Especially the doc skill, whose docx-js
    // code can take many seconds to write.
    case "tool:preparing": {
      const n = (ev.name || "").toLowerCase();
      conv.statusLine =
        n.includes("get_document_skill") ? "Reading the formatting rules…" :
        n.includes("build_document")     ? "Designing your document…" :
        n.includes("generate_document")  ? "Preparing your document…" :
        n.includes("run_code")           ? "Running a quick calculation…" :
        (n.includes("outlook") || n.includes("mail")) ? "Working with your email…" :
        n.includes("teams")              ? "Working with Microsoft Teams…" :
        n.includes("calendar")           ? "Checking your calendar…" :
        n.includes("remember")           ? "Updating memory…" :
        (n.includes("list_files") || n.endsWith("__read") || n === "read") ? "Reading your files…" :
        n.includes("__")                 ? "Working with your connected apps…" :
        "Working…";
      break;
    }
    // Live progress while the agent writes a long tool input (e.g. the document
    // build code) — proves it's actively working, not frozen.
    case "tool:progress": {
      const n = (ev.name || "").toLowerCase();
      const base = n.includes("build_document")    ? "Designing your document"
                 : n.includes("generate_document") ? "Preparing your document"
                 : "Working";
      const c = ev.chars || 0;
      const amt = c >= 1000 ? `${(c / 1000).toFixed(1)}k` : `${c}`;
      conv.statusLine = `${base}… (${amt} characters written)`;
      break;
    }
    case "tool:done":
    case "tool:fail": {
      if (!a) break;
      const status = ev.type === "tool:fail" ? "fail" : "done";
      for (let i = a.blocks.length - 1; i >= 0; i--) {
        const b = a.blocks[i];
        if (b.kind === "tool" && b.name === ev.name && b.status === "running") {
          b.status = status; if (ev.detail) b.detail = ev.detail; break;
        }
      }
      break;
    }
    case "diff:header":
      if (a) a.blocks.push({ kind: "diff", path: ev.path, added: ev.added, removed: ev.removed, isNew: ev.isNew, lines: [] });
      break;
    case "diff:line": {
      if (!a) break;
      const last = a.blocks[a.blocks.length - 1];
      if (last && last.kind === "diff") last.lines.push({ kind: ev.kind, line: ev.line });
      break;
    }
    case "command": if (a) a.blocks.push({ kind: "command", cmd: ev.cmd }); break;
    // Office-specific: a generated document is ready → render a download card. The
    // worker (doc_worker.py) produces it; the agent never auto-delivers raw bytes.
    case "document":
      if (a) a.blocks.push({ kind: "doc", jobId: ev.jobId || ev.job_id, format: ev.format || ev.fmt, filename: ev.filename || ev.name || `document.${ev.format || ev.fmt || "pdf"}` });
      break;
    case "notice":
      if (a && ev.level === "warn") a.blocks.push({ kind: "notice", msg: ev.msg, level: "warn" });
      break;
    case "error":
      // Compliance HARD-BLOCK on an outbound write/send surfaces here as an error
      // block — the user is told why, never left guessing.
      // NOTE: we deliberately do NOT set conv.status="error" here. A bare
      // {type:"error"} event (tool failure, compliance block) means the CLI is
      // still alive — the session has context and the user can continue. Only
      // {type:"result", status:"error"} and {type:"session:exit"} change status,
      // because those signal the CLI has finished or exited.
      if (a) a.blocks.push({ kind: "notice", msg: ev.msg, level: "error" });
      break;
    case "phase":      conv.statusLine = `Phase: ${ev.phase}`; break;
    case "agent:iter": conv.statusLine = `Working — step ${ev.iter}/${ev.max} (${ev.phase})`; break;
    case "agent:ttfb": conv.statusLine = "Thinking…"; break;
    // A connector send or document write is pending approval — never auto-runs.
    case "confirm":    conv.pendingConfirm = { id: ev.id, label: ev.label, tool: ev.tool, detail: ev.detail }; break;
    case "__clear_confirm": conv.pendingConfirm = null; break;
    case "session:init":
      if (ev.model) conv.model = ev.model;
      if (ev.permissionMode) conv.permissionMode = ev.permissionMode;
      if (Array.isArray(ev.slashCommands) && ev.slashCommands.length) {
        const norm = ev.slashCommands.map((c) =>
          typeof c === "string"
            ? { name: c, description: "", argumentHint: "" }
            : { name: c.name, description: c.description || "", argumentHint: c.argumentHint || "" }
        );
        const hasDesc = norm.some((c) => c.description);
        if (!conv.slashCommands?.length || hasDesc) conv.slashCommands = norm;
      }
      break;
    case "context":
      conv.contextPct = ev.pct; conv.contextTokens = ev.tokens; conv.contextMax = ev.max;
      break;
    case "result": {
      if (a) {
        if (ev.status === "ok" && typeof ev.response === "string" && ev.response.trim()) {
          // Reconcile streamed text to the canonical final response.
          const idx = a.blocks.findIndex((b) => b.kind === "text");
          if (idx >= 0) {
            a.blocks[idx] = { kind: "text", text: ev.response };
            a.blocks = a.blocks.filter((b, i) => b.kind !== "text" || i === idx);
          } else {
            a.blocks.unshift({ kind: "text", text: ev.response });
          }
        }
        if (ev.status === "error") a.blocks.push({ kind: "notice", msg: ev.error || "Run failed", level: "error" });
        a.streaming = false;
        a.cost = {
          usd: ev.costUsd, totalUsd: ev.costTotalUsd,
          input: ev.usage?.input, output: ev.usage?.output, elapsedMs: ev.elapsedMs,
        };
        a.modelLabel = ev.model ? MODEL_LABEL(ev.model) : (conv.model ? MODEL_LABEL(conv.model) : null);
      }
      if (typeof ev.costTotalUsd === "number") conv.costTotal = ev.costTotalUsd;
      if (ev.model) conv.model = ev.model;
      conv.status = ev.status === "ok" ? "done" : ev.status === "interrupted" ? "idle" : "error";
      conv.statusLine = "";
      conv.pendingConfirm = null;
      break;
    }
    case "resume:id": conv.resumeId = ev.resumeId; break;
    case "session:exit": conv.status = "exited"; conv.statusLine = ""; break;
    default: break;
  }
}

function reducer(state, action) {
  switch (action.type) {
    case "ADD": {
      return { convs: { ...state.convs, [action.conv.id]: action.conv } };
    }
    case "USER_TURN": {
      const conv = state.convs[action.id];
      if (!conv) return state;
      const next = {
        ...conv,
        status: "running",
        // Show the spinner the instant the message is sent — don't wait for the
        // agent:ttfb event (which can be seconds away). Otherwise the pane looks
        // frozen between send and first token.
        statusLine: "Thinking…",
        messages: [
          ...conv.messages,
          { role: "user", blocks: [{ kind: "text", text: action.text }], attachments: action.attachments || [] },
          { role: "assistant", blocks: [], streaming: true },
        ],
      };
      return { convs: { ...state.convs, [action.id]: next } };
    }
    case "EVENT": {
      const conv = state.convs[action.id];
      if (!conv) return state;
      const clone = {
        ...conv,
        messages: conv.messages.map((m) =>
          m.role === "assistant" ? { ...m, blocks: m.blocks.map((b) => ({ ...b, lines: b.lines ? [...b.lines] : undefined })) } : m
        ),
      };
      applyEvent(clone, action.event);
      return { convs: { ...state.convs, [action.id]: clone } };
    }
    default: return state;
  }
}

// ── Block renderers ────────────────────────────────────────────────────────────

// Normalize MCP-qualified tool names for display: strip the server prefix
// and replace any legacy "cowork" server name with "buddy".
function formatToolName(name) {
  if (!name) return name;
  return name
    .replace(/^mcp__ainxt_cowork__/, "buddy__")
    .replace(/^ainxt_cowork__/, "buddy__")
    .replace(/^mcp__ainxt_buddy__/, "buddy__")
    .replace(/^ainxt_buddy__/, "buddy__");
}

function ToolChip({ b }) {
  const Icon = b.status === "running" ? Loader2 : b.status === "fail" ? XCircle : CheckCircle2;
  const color = b.status === "running" ? "text-blue-600" : b.status === "fail" ? "text-red-600" : "text-emerald-600";
  return (
    <div className="flex items-center gap-2 text-xs px-2 py-1 font-mono">
      <Icon className={`w-3.5 h-3.5 shrink-0 ${color} ${b.status === "running" ? "animate-spin" : ""}`} />
      <span className="text-gray-700 shrink-0">{formatToolName(b.name)}</span>
      {b.detail && <span className="text-gray-400 truncate">· {b.detail}</span>}
    </div>
  );
}

// Collapsible panel for a run of consecutive tool calls (connector reads, document
// reads, KB searches) — keeps the answer prominent.
function ToolGroup({ items }) {
  const running = items.some((t) => t.status === "running");
  const [open, setOpen] = useState(true);
  const Icon = running ? Loader2 : CheckCircle2;
  const color = running ? "text-blue-600" : "text-emerald-600";
  const n = items.length;
  const label = running ? `Working… ${n} step${n !== 1 ? "s" : ""}` : `${n} tool call${n !== 1 ? "s" : ""}`;
  return (
    <div className="my-2 border border-gray-200 rounded-lg overflow-hidden bg-gray-50/60">
      <button onClick={() => setOpen((o) => !o)} className="w-full flex items-center gap-2 px-2.5 py-1.5 text-xs hover:bg-gray-100 transition">
        <Icon className={`w-3.5 h-3.5 ${color} ${running ? "animate-spin" : ""}`} />
        <span className="text-gray-600 font-medium">{label}</span>
        <span className="ml-auto text-gray-400">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="max-h-56 overflow-y-auto border-t border-gray-200 divide-y divide-gray-100 bg-white">
          {items.map((b, i) => <ToolChip key={i} b={b} />)}
        </div>
      )}
    </div>
  );
}

function Diff({ b }) {
  return (
    <div className="my-2 border border-gray-200 rounded-md overflow-hidden">
      <div className="flex items-center gap-2 bg-gray-100 px-2 py-1 text-xs">
        <FileDiff className="w-3.5 h-3.5 text-indigo-600" />
        <span className="font-mono text-gray-700">{b.path}</span>
        {b.isNew && <span className="text-emerald-600">new</span>}
        <span className="ml-auto text-emerald-600">+{b.added}</span>
        <span className="text-red-600">-{b.removed}</span>
      </div>
      <pre className="text-xs font-mono overflow-x-auto max-h-72 overflow-y-auto bg-white m-0">
        {b.lines.map((l, i) => (
          <div key={i} className={
            l.kind === "+" ? "bg-emerald-50 text-emerald-800" :
            l.kind === "-" ? "bg-red-50 text-red-800" :
            l.kind === "@@" ? "bg-indigo-50 text-indigo-700" : "text-gray-600"
          }>
            <span className="px-2 inline-block w-full whitespace-pre">{l.kind === "@@" ? "" : l.kind}{l.line}</span>
          </div>
        ))}
      </pre>
    </div>
  );
}

// A mutating tool (Edit/Write/MultiEdit on a local file) shown as a diff card with
// apply status. Collapsible; defaults open while running so the user sees the
// proposed change next to the permission prompt.
function ToolDiff({ b }) {
  const d = b.diff;
  const [open, setOpen] = useState(true);
  const status = b.status; // running | done | fail
  const StatusIcon = status === "running" ? Loader2 : status === "fail" ? XCircle : CheckCircle2;
  const statusColor = status === "running" ? "text-blue-600" : status === "fail" ? "text-red-600" : "text-emerald-600";
  const verb = b.name === "Write" ? "Write" : b.name === "MultiEdit" ? "Edit" : "Edit";
  return (
    <div className="my-2 border border-gray-200 rounded-md overflow-hidden">
      <button onClick={() => setOpen((o) => !o)} className="w-full flex items-center gap-2 bg-gray-100 px-2 py-1.5 text-xs hover:bg-gray-200/70 transition">
        <StatusIcon className={`w-3.5 h-3.5 shrink-0 ${statusColor} ${status === "running" ? "animate-spin" : ""}`} />
        <FileDiff className="w-3.5 h-3.5 text-indigo-600 shrink-0" />
        <span className="text-gray-700 shrink-0">{verb}</span>
        <span className="font-mono text-gray-500 truncate">{d.path || d.name}</span>
        {d.isNew && <span className="text-emerald-600 shrink-0">new</span>}
        <span className="ml-auto text-emerald-600 shrink-0">+{d.added}</span>
        <span className="text-red-600 shrink-0">-{d.removed}</span>
        <span className="text-gray-400 shrink-0">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <pre className="text-xs font-mono overflow-x-auto max-h-80 overflow-y-auto bg-white m-0 leading-5">
          {d.lines.map((l, i) => (
            <div key={i} className={
              l.kind === "+" ? "bg-emerald-50 text-emerald-800" :
              l.kind === "-" ? "bg-red-50 text-red-800" :
              l.kind === "@@" ? "bg-indigo-50 text-indigo-700" : "text-gray-600"
            }>
              <span className="px-2 inline-block w-full whitespace-pre">{l.kind === "@@" ? "⋯" : l.kind}{l.line}</span>
            </div>
          ))}
          {d.truncated > 0 && (
            <div className="text-gray-400"><span className="px-2 inline-block">… {d.truncated} more line{d.truncated !== 1 ? "s" : ""}</span></div>
          )}
        </pre>
      )}
    </div>
  );
}

// Hide the raw tool-call plumbing (tool_use / diffs / commands) from Buddy users —
// they should see clean results, not the agent's internal steps. A single subtle
// Show collapsible tool timeline (tool name + detail) while the agent is working.
// Each tool appears as a chip with a spinner while running, then a check/cross when done.
const HIDE_TOOL_DISPLAYS = false;

function Blocks({ blocks, settled }) {
  // Coalesce consecutive read-only tool calls into one collapsible ToolGroup so a
  // turn with many document/connector reads doesn't push the answer off-screen.
  const out = [];
  let i = 0;
  // Track whether we emitted any VISIBLE output (text/doc/notice). With tool
  // displays hidden, a turn that only ran tools would otherwise render an empty
  // bubble — we backfill a minimal "Done" line once it's settled (G13).
  let visibleOut = false;
  while (i < blocks.length) {
    const b = blocks[i];
    // ── Suppressed tool plumbing (#2) ──────────────────────────────────────────
    if (HIDE_TOOL_DISPLAYS && (b.kind === "tool" || b.kind === "diff" || b.kind === "command")) {
      // Collapse the whole contiguous run of tool/diff/command blocks. If any is
      // still running, emit ONE "Working…" line; otherwise emit nothing.
      let anyRunning = false;
      while (i < blocks.length && (blocks[i].kind === "tool" || blocks[i].kind === "diff" || blocks[i].kind === "command")) {
        if (blocks[i].kind === "tool" && blocks[i].status === "running") anyRunning = true;
        i++;
      }
      if (anyRunning) {
        out.push(
          <div key={`w${i}`} className="flex items-center gap-2 text-xs text-gray-400 my-1">
            <span className="inline-block w-3 h-3 border-2 border-gray-300 border-t-gray-500 rounded-full animate-spin" />
            <span>Working…</span>
          </div>
        );
      }
      continue;
    }
    if (b.kind === "tool" && b.diff) {
      out.push(<ToolDiff key={i} b={b} />);
      i++;
      continue;
    }
    if (b.kind === "tool") {
      const group = [];
      while (i < blocks.length && blocks[i].kind === "tool" && !blocks[i].diff) { group.push(blocks[i]); i++; }
      out.push(<ToolGroup key={`g${i}`} items={group} />);
      continue;
    }
    if (b.kind === "text") {
      // The agent embeds [DOCJOB:id:fmt:name] markers INLINE in its reply text;
      // parse them out and render each as the shared download card (the rest as
      // markdown). Without this the marker shows as raw text. (parity with Message.jsx)
      if (b.text) {
        const parts = parseDocMarkers(b.text);
        out.push(
          <div key={i} className="md-body">
            {parts.map((p, j) => {
              if (p.type === "docjob")
                return <DocDownloadButton key={`doc-${p.jobId}`} jobId={p.jobId} format={p.format} filename={p.filename} />;
              if (p.type === "text" && p.value.trim())
                return (
                  <ReactMarkdown key={`t${j}`} remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeHighlight, rehypeKatex]} components={mdComponents}>
                    {p.value}
                  </ReactMarkdown>
                );
              return null;
            })}
          </div>
        );
        visibleOut = true;
      }
    } else if (b.kind === "doc") {
      // Generated Word/Excel/PPT/PDF — rendered as the shared download card.
      out.push(<DocDownloadButton key={`doc-${b.jobId}`} jobId={b.jobId} format={b.format} filename={b.filename} />);
      visibleOut = true;
    } else if (b.kind === "diff") {
      out.push(<Diff key={i} b={b} />);
    } else if (b.kind === "command") {
      out.push(
        <div key={i} className="flex items-center gap-2 text-xs bg-gray-900 text-gray-100 rounded-md px-2 py-1 my-1 font-mono">
          <Terminal className="w-3.5 h-3.5 text-emerald-400 shrink-0" /><span className="truncate">{b.cmd}</span>
        </div>
      );
    } else if (b.kind === "notice") {
      out.push(
        <div key={i} className={`text-xs rounded-md px-2 py-1 my-1 ${b.level === "error" ? "bg-red-50 text-red-700 border border-red-200" : "bg-amber-50 text-amber-700 border border-amber-200"}`}>
          {b.msg}
        </div>
      );
      visibleOut = true;
    }
    i++;
  }
  // G13: a settled turn that only ran tools (all suppressed) would render an empty
  // bubble — backfill a minimal completion line so it never looks blank/broken.
  if (settled && !visibleOut && blocks.length > 0) {
    out.push(
      <div key="done" className="flex items-center gap-1.5 text-xs text-gray-400 my-1">
        <Check size={13} className="text-emerald-500" /><span>Done.</span>
      </div>
    );
  }
  return <>{out}</>;
}

function MessageRow({ m, isLast, onRegenerate, busy }) {
  const [copied, setCopied] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [downloadingId, setDownloadingId] = useState(null);
  // Re-download a previously uploaded attachment via the existing, already-
  // working backend endpoint (GET /chat/attachments/{id}/raw). Only files with
  // a serverId (i.e. actually uploaded to the server, not just locally
  // extracted) can be re-fetched this way — see attachments' shape comment
  // at the USER_TURN dispatch site.
  const handleDownloadAttachment = async (a) => {
    if (!a.serverId || downloadingId) return;
    setDownloadingId(a.id);
    try {
      const r = await authFetch(`${API_BASE}/chat/attachments/${a.serverId}/raw`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = a.name || "attachment";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      // Best-effort — e.g. attachment expired/deleted server-side. No
      // dedicated error UI here (matches the chip's existing minimal style);
      // the click simply does nothing rather than throwing into the console.
    } finally {
      setDownloadingId(null);
    }
  };
  if (m.role === "user") {
    return (
      <div className="flex justify-end mb-6">
        <div className="bg-gray-100 px-4 py-3 rounded-md text-sm max-w-4xl whitespace-pre-wrap text-gray-800">
          {m.blocks[0]?.text}
          {m.attachments?.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1.5">
              {m.attachments.map((a) => (
                a.serverId ? (
                  <button
                    key={a.id}
                    type="button"
                    onClick={() => handleDownloadAttachment(a)}
                    disabled={downloadingId === a.id}
                    title={`Download ${a.name}`}
                    className="flex items-center gap-1 text-[11px] bg-white/70 border border-gray-200 rounded px-1.5 py-0.5 text-gray-600 hover:bg-white hover:border-gray-300 hover:text-gray-800 transition-colors cursor-pointer disabled:opacity-50"
                  >
                    {downloadingId === a.id
                      ? <Loader2 size={10} className="animate-spin" />
                      : <Download size={10} />}
                    {a.name}
                  </button>
                ) : (
                  <span key={a.id} className="text-[11px] bg-white/70 border border-gray-200 rounded px-1.5 py-0.5 text-gray-600">{a.name}</span>
                )
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }
  const msgText = () => (m.blocks || []).filter((b) => b.kind === "text").map((b) => b.text).join("\n").trim();
  const handleCopy = () => {
    const t = msgText(); if (!t) return;
    navigator.clipboard?.writeText(t).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }).catch(() => {});
  };
  const handleSpeak = () => {
    if (typeof window === "undefined" || !window.speechSynthesis) return;
    if (speaking) { window.speechSynthesis.cancel(); setSpeaking(false); return; }
    const t = msgText(); if (!t) return;
    const u = new SpeechSynthesisUtterance(t.slice(0, 4000));
    u.onend = () => setSpeaking(false); u.onerror = () => setSpeaking(false);
    window.speechSynthesis.speak(u); setSpeaking(true);
  };
  const c = m.cost;
  const _hasText = (m.blocks || []).some((b) => b.kind === "text" && b.text && b.text.trim());
  return (
    <div className="flex justify-start mb-6">
      <div className="px-4 py-3 rounded-md text-sm w-full max-w-4xl text-gray-800">
        <Blocks blocks={m.blocks} settled={!m.streaming} />
        {/* Action bar + meta pills (Chat-equivalent) — assistant, once settled AND
            there's actual text to copy/read/regenerate (a tool-only turn has none). */}
        {!m.streaming && _hasText && (
          <>
            <div className="flex items-center gap-1 mt-2 pt-1">
              <button onClick={handleCopy} title="Copy response"
                className="p-1 rounded cursor-pointer text-gray-600 hover:text-gray-800 transition-colors">
                {copied ? <Check size={14} className="text-green-500" /> : <Copy size={14} />}
              </button>
              <button onClick={handleSpeak} title={speaking ? "Stop reading" : "Read aloud"}
                className={`p-1 rounded cursor-pointer transition-colors ml-0.5 ${speaking ? "text-blue-500 animate-pulse" : "text-gray-600 hover:text-blue-500"}`}>
                {speaking ? <VolumeX size={14} /> : <Volume2 size={14} />}
              </button>
              {isLast && (
                <button onClick={onRegenerate} disabled={busy} title="Regenerate response"
                  className="p-1 rounded cursor-pointer text-gray-600 hover:text-purple-500 transition-colors ml-0.5 disabled:opacity-30">
                  <RotateCcw size={14} />
                </button>
              )}
            </div>
            <MessageMeta msg={{
              role: "assistant", streaming: false,
              // Model display is hidden throughout Buddy — always omit the per-message
              // model badge (MessageRow has no access to the lock state, and Buddy is
              // model-locked anyway). Tokens/cost/latency are kept.
              modelLabel: null,
              inTok: c?.input ?? null, outTok: c?.output ?? null,
              costUsd: c?.usd ?? null,
              latency: c?.elapsedMs ? c.elapsedMs / 1000 : null,
            }} />
          </>
        )}
        {/* In-message "Thinking…" dots removed — the single status indicator is the
            bottom statusLine (spinner + meaningful message: Thinking…/Reading your
            files…/Designing your document…). Two stacked indicators looked bad. */}
      </div>
    </div>
  );
}

// Inline permission strip — shown just above the input box (never a modal overlay).
// For office work this fronts the confirm-and-send / write
// gate: connector sends + document writes pause here and ONLY run after approval.
function PermissionBar({ conv, onAnswer }) {
  if (!conv?.pendingConfirm) return null;
  const { id, tool, detail } = conv.pendingConfirm;
  const sid = conv.id;
  return (
    <div className="mb-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2">
      <div className="flex items-center gap-2 min-w-0">
        <ShieldQuestion className="w-4 h-4 shrink-0 text-amber-600" />
        <span className="text-sm text-gray-800 min-w-0">
          <span className="font-medium">{tool || "Action"}</span>
          {detail ? <span className="text-gray-500"> · <code className="text-[12px]">{detail}</code></span> : null}
        </span>
        <div className="flex-1" />
        <button onClick={() => onAnswer(sid, id, "no")}
          className="px-2.5 py-1 text-xs rounded-md border border-gray-300 text-gray-700 bg-white hover:bg-gray-50">Deny</button>
        <button onClick={() => onAnswer(sid, id, "always")}
          className="px-2.5 py-1 text-xs rounded-md border border-amber-400 text-amber-800 bg-white hover:bg-amber-100">Always allow</button>
        <button onClick={() => onAnswer(sid, id, "yes")}
          className="px-2.5 py-1 text-xs rounded-md bg-indigo-600 text-white hover:bg-indigo-700">Allow</button>
      </div>
    </div>
  );
}

// ── Main view ──────────────────────────────────────────────────────────────────
export default function CoworkDesktop() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [folder, setFolder] = useState(null);          // OPTIONAL working dir
  const [attachedFiles, setAttachedFiles] = useState([]); // files attached to the next message
  const [attaching, setAttaching] = useState(false);      // true while processing attached files
  // Per-file upload percentage while attaching (only meaningful once a file has
  // reached the server-upload tier — earlier local-parsing tiers have no byte
  // progress to report, so they fall back to the indeterminate bar below).
  const [attachProgress, setAttachProgress] = useState({}); // { [fileName]: 0-100 }
  const [fileLimitError, setFileLimitError] = useState(false); // too many files selected at once
  const [imageBlockedError, setImageBlockedError] = useState(false); // image file(s) rejected via paperclip attach
  // Model lock policy from the gateway (ops-configurable). Falls back to the pinned
  // defaults until /buddy/model-config resolves.
  const [modelLocked, setModelLocked] = useState(DEFAULT_MODEL_LOCKED);
  const [lockedModel, setLockedModel] = useState(DEFAULT_LOCKED_MODEL);
  const [model, setModel] = useState(DEFAULT_MODEL_LOCKED ? DEFAULT_LOCKED_MODEL : MODEL_DEFAULT);
  // ── Full model catalog — SAME source Chat.jsx uses ──────────────────────
  // allModelProviders: raw GET /all-models response (every provider the platform
  // can route to: Claude, OpenAI, Google/Gemini, Local/in-house — whatever
  // feature flags are on server-side). allowedModels + governanceLoaded: GET
  // /model-governance/my-models, used to filter the flattened list down to what
  // this user/department is permitted (identical semantics to Chat.jsx).
  const [allModelProviders, setAllModelProviders] = useState([]);
  const [allowedModels, setAllowedModels] = useState([]);
  const [governanceLoaded, setGovernanceLoaded] = useState(false);
  // `models` is the flattened, governance-filtered, {key,label}-shaped list the
  // rest of this file (picker render, /model command, MODEL_LABEL_FROM) already
  // consumes — derived below once allModelProviders/allowedModels are known.
  // Falls back to BASE_MODELS (Claude-only) until /all-models responds, so the
  // picker/​`/model` command never render empty and behave exactly as before
  // for any deployment that can't reach that endpoint.
  const models = useMemo(() => {
    if (!allModelProviders.length) return BASE_MODELS;
    // Deliberately omit "Auto" — Buddy's /v1/messages route has no complexity-
    // based auto-routing (unlike Chat's /ask), so an "auto" hint would just
    // silently fall through to the Claude-primary default. Matches Code.jsx's
    // picker, which also lists only concrete model ids.
    const flattened = allModelProviders
      .filter((g) => (g.provider || "").toLowerCase() !== "auto")
      .flatMap((g) => (g.models || []).map((m) => ({
        key: m.modelId || m.id,
        label: m.label || (m.id?.startsWith("local:") ? _localLabel(m.id) : m.id),
        modelId: m.modelId || m.id,
        tier: m.tier,
        provider: g.provider,
      })));
    if (!flattened.length) return BASE_MODELS;
    // Fail-open (show everything) until governance has loaded; once loaded, an
    // empty allowedModels means every model is blocked for this user — same
    // semantics as Chat.jsx.
    if (!governanceLoaded) return flattened;
    return flattened.filter((m) => allowedModels.includes(m.modelId || m.key));
  }, [allModelProviders, allowedModels, governanceLoaded]);
  const [permMode, setPermMode] = useState("default");
  const [roles, setRoles] = useState([]);              // installable plugins/role specialists
  const [roleId, setRoleId] = useState("");            // selected role ("" = generic Buddy)
  const roleRef = useRef(null);                        // current role payload for new sessions
  // Scheduler panel (dedicated view lives in CoworkScheduler.jsx). `showSchedule`
  // opens it straight into the create form (via the /schedule slash command);
  // `showSchedules` opens the list. `schedulePrompt` seeds the create form.
  const [showSchedule, setShowSchedule] = useState(false);
  const [schedulePrompt, setSchedulePrompt] = useState("");
  const [toast, setToast] = useState("");   // transient success banner (e.g. after scheduling)
  const [showSchedules, setShowSchedules] = useState(false);
  // React-based dialogs (avoid native window.confirm/alert, which corrupt the
  // Electron renderer's keyboard focus and freeze subsequent text inputs).
  const { confirm } = useConfirm();
  const { toast: uiToast } = useToast();
  // ── Prompt queue (Buddy-only) ──────────────────────────────
  // maxWait: fetched from GET /buddy/queue-config (env BUDDY_QUEUE_MAX_WAIT, default 5).
  // queuedCount: separate state so the UI indicator re-renders on changes.
  // queueExpanded: controls the collapsed/expanded state of the queue list.
  const [maxWait, setMaxWait] = useState(5);
  const [queuedCount, setQueuedCount] = useState(0);
  const [queueExpanded, setQueueExpanded] = useState(false);
  const { enqueue, dequeueNext, removeAt, clearQueue, getQueue, isFull } = usePromptQueue(maxWait);
  // Projects + persistent memory
  const [projects, setProjects] = useState([]);   // loaded from the server (cowork_projects)
  const [activeProjectId, setActiveProjectId] = useState("");
  const projectRef = useRef(null);
  const activeProjectIdRef = useRef("");   // stamped onto each task at creation
  const [editingProject, setEditingProject] = useState(null); // {id?, name, instructions, memory}
  // Durable per-user memory (prefs + agent-saved facts) — what Buddy remembers about you
  const [showMemory, setShowMemory] = useState(false);
  const [memPrefs, setMemPrefs] = useState(null);   // {role, tone, default_doc_format, email_signature, memory_notes:[]}
  const [memNote, setMemNote] = useState("");       // new fact being typed
  const [memBusy, setMemBusy] = useState(false);
  const [auth, setAuth] = useState({ authenticated: false, error: "loading" });
  const [loginLog, setLoginLog] = useState("");
  const [loggingIn, setLoggingIn] = useState(false);
  const [adoptError, setAdoptError] = useState("");   // user-friendly silent-adopt failure
  const [manualKey, setManualKey] = useState("");      // API key entered manually by the user
  const [manualKeyError, setManualKeyError] = useState("");
  const [showLoginDetails, setShowLoginDetails] = useState(false); // toggle raw CLI log
  const [loginUrl, setLoginUrl] = useState("");        // device-code URL parsed from loginLog

  const [chatId, setChatId] = useState(null);          // ephemeral live CLI session id
  const [convId, setConvId] = useState(null);          // stable conversation id (persisted)
  const [chatInput, setChatInput] = useState("");
  const [conversations, setConversations] = useState([]); // server-persisted history (loaded on mount)
  const [files, setFiles] = useState([]);               // optional folder file list for @-mentions
  const [compIdx, setCompIdx] = useState(0);            // highlighted completion-menu row
  const [compDismissed, setCompDismissed] = useState(false);
  const chatScrollRef = useRef(null);
  const textareaRef = useRef(null);
  const compItemRef = useRef(null);
  // Hidden <input type="file"> — used by attachFile() to get real File objects
  // (with bytes in memory) from the browser, bypassing the IPC path entirely.
  // In Electron, File objects have a .path property (absolute disk path) so we
  // get both the bytes (for server upload) and the path (for agent reference).
  const fileInputRef = useRef(null);
  // Stick-to-bottom auto-scroll: follow new content ONLY when the user is already
  // near the bottom. The moment they scroll up, we stop auto-scrolling (respecting
  // their position); when they return to the bottom, stickiness resumes. A "Jump to
  // latest" button appears while scrolled up.
  const [atBottom, setAtBottom] = useState(true);
  const atBottomRef = useRef(true);
  const _NEAR_BOTTOM_PX = 120;
  const onChatScroll = useCallback(() => {
    const el = chatScrollRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < _NEAR_BOTTOM_PX;
    atBottomRef.current = near;
    setAtBottom((prev) => (prev === near ? prev : near));
  }, []);
  const scrollToBottom = useCallback((behavior = "smooth") => {
    const el = chatScrollRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior });
    atBottomRef.current = true;
    setAtBottom(true);
  }, []);
  const folderRef = useRef(null);
  const convIdRef = useRef(null);
  const chatIdRef = useRef(null);
  const convsRef = useRef(state.convs);
  const openingRef = useRef(false);
  const primedRef = useRef(new Set());
  // True while openConversation is mid-switch (between persistCurrent() and the
  // final setChatId/setConvId). Guards persistCurrent so it never fires with stale
  // refs during the async gap — which would save the INCOMING conversation's
  // messages under the OUTGOING conversation's id (the session-overwrite bug).
  const _switchingConvRef = useRef(false);
  // CLI-missing REST fallback: remember the last DOCUMENT request per session so
  // that if the CLI binary is absent (session:exit code -1) we can re-route it
  // through POST /docs/generate instead of hard-failing. Keyed by session id.
  const pendingDocReqRef = useRef({});
  // Maps conversation.id → { chatId: liveSessionId, resumeId: agentSessionId }.
  // Lets us REATTACH to a still-live session on return (no new process, running
  // task keeps going) or --resume the agent context cold after an app restart.
  const sessionMapRef = useRef({});
  // convId → agent session_id (for persisting resume_id even before a full save).
  const resumeMapRef = useRef({});
  // Ref bridge so the mount-once event subscription can call the latest
  // submitDocFallback closure without re-subscribing.
  const submitDocFallbackRef = useRef(null);
  useEffect(() => { folderRef.current = folder; }, [folder]);
  useEffect(() => { convIdRef.current = convId; }, [convId]);
  useEffect(() => { chatIdRef.current = chatId; }, [chatId]);
  useEffect(() => { convsRef.current = state.convs; }, [state.convs]);
  // Auto-dismiss the file-limit error banner after 5 seconds (mirrors Chat.jsx).
  useEffect(() => {
    if (!fileLimitError) return;
    const t = setTimeout(() => setFileLimitError(false), 5000);
    return () => clearTimeout(t);
  }, [fileLimitError]);
  // Auto-dismiss the image-blocked error banner after 5 seconds (same pattern).
  useEffect(() => {
    if (!imageBlockedError) return;
    const t = setTimeout(() => setImageBlockedError(false), 5000);
    return () => clearTimeout(t);
  }, [imageBlockedError]);
  // Clear the last-conversation cursor on every fresh app launch so the restore
  // effect below never auto-reopens a previous session. The old behaviour
  // (auto-restore) caused the CLI to resume the prior conversation's context,
  // making the model reply with stale history (e.g. a document-generation
  // failure message) when the user typed "Hi" in what they expected to be a
  // new session. Prior conversations remain accessible in the sidebar.
  // This runs once on mount (empty dep array) — not on every convId change.
  useEffect(() => {
    try { localStorage.removeItem(LAST_CONV_KEY); } catch { /* ignore */ }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Track the active conversation id in localStorage WITHIN a session so that
  // switching tabs and returning (without a full app restart) restores the user's
  // place. Cleared on mount above so it never survives across app launches.
  useEffect(() => {
    try {
      if (convId) localStorage.setItem(LAST_CONV_KEY, convId);
    } catch { /* ignore */ }
  }, [convId]);

  // Persist the current conversation NOW (uses refs → never stale).
  // Fix #21/#27/#29: this now also persists WHILE the conversation is running (so a
  // navigation/screen-switch mid-task no longer loses the in-flight conversation),
  // and is invoked from a periodic flush + on unmount below.
  const persistCurrent = useCallback(() => {
    // Do NOT persist while openConversation is mid-switch: convIdRef/chatIdRef still
    // point to the OUTGOING conversation but convsRef may already contain the
    // INCOMING conversation's messages — saving now would overwrite the outgoing
    // conversation's history with the incoming one's content (the overwrite bug).
    if (_switchingConvRef.current) return;
    const f = folderRef.current, cid = convIdRef.current, sid = chatIdRef.current;
    if (!cid || !sid) return;                 // folder is optional — don't gate on it
    const conv = convsRef.current[sid];
    if (!conv || !conv.messages?.length) return;
    const firstUser = conv.messages.find((m) => m.role === "user");
    const title = cleanConvTitle(firstUser?.blocks?.[0]?.text || "Conversation");
    convSave({ id: cid, title, messages: conv.messages, projectId: conv.projectId || "",
               folder: f, resumeId: conv.resumeId || resumeMapRef.current[cid] || null });
  }, []);

  // `conversations` alone can't tell "fetch hasn't returned yet" apart from
  // "fetch returned an empty list" (convList() resolves to [] on either a genuine
  // empty history OR a network error) — we need a separate loaded flag so the
  // restore effect below can tell the difference and never wait forever.
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const refreshConversations = useCallback(() => {
    convList().then((list) => { setConversations(list); setHistoryLoaded(true); });
  }, []);
  useEffect(() => { refreshConversations(); }, [refreshConversations]);  // load history from server on mount

  // Restore the last-open conversation ONCE on mount, so a remount / reload / long
  // tab-away lands the user back where they were instead of a blank new chat. Uses
  // the existing openConversation path (reattach → --resume → show saved messages),
  // via a ref bridge because openConversation is defined below. Guards: only when
  // nothing is already open, and only if the stored id is in the loaded list (so a
  // deleted conversation isn't re-opened).
  //
  // G-race: this used to race the pre-warm effect below — pre-warm's local auth
  // check resolves near-instantly while this effect waits on a server round trip
  // (GET /buddy/conversations), so pre-warm almost always won, set chatId to a
  // FRESH blank session first, and this effect's "already somewhere" guard then
  // permanently gave up — even once the real history arrived a moment later. That
  // stranded desktop Buddy on an empty chat after every close/reopen while the
  // conversation (and everything sent afterward) silently kept accumulating on an
  // orphaned session nobody was looking at. `restoreAttempted` + `restoringRef`
  // let pre-warm below wait until this effect has actually settled (restored or
  // decided there's nothing to restore) before it's allowed to spawn a blank one.
  const openConversationRef = useRef(null);
  const _restoredRef = useRef(false);
  const restoringRef = useRef(false);
  const [restoreAttempted, setRestoreAttempted] = useState(false);
  useEffect(() => {
    if (_restoredRef.current) return;
    if (chatId || convId) { _restoredRef.current = true; setRestoreAttempted(true); return; }  // already somewhere
    if (!historyLoaded) return;                                     // fetch still in flight
    _restoredRef.current = true;
    let lastId = null;
    try { lastId = localStorage.getItem(LAST_CONV_KEY); } catch { /* ignore */ }
    const target = lastId ? conversations.find((c) => c.id === lastId) : null;
    if (target && openConversationRef.current) {
      restoringRef.current = true;
      Promise.resolve(openConversationRef.current(target)).finally(() => {
        restoringRef.current = false;
        setRestoreAttempted(true);
      });
    } else {
      setRestoreAttempted(true);
    }
  }, [historyLoaded, conversations, chatId, convId]);

  // Periodic autosave + save-on-unmount so history is never lost when switching
  // screens (the tester saw chats cleared after 1–2 min / on navigation). Runs
  // every 20s and once more when the component unmounts. (#27/#29)
  useEffect(() => {
    const iv = setInterval(() => { try { persistCurrent(); } catch { /* ignore */ } }, 20000);
    const onHide = () => { try { persistCurrent(); } catch { /* ignore */ } };
    window.addEventListener("beforeunload", onHide);
    document.addEventListener("visibilitychange", onHide);
    return () => {
      clearInterval(iv);
      window.removeEventListener("beforeunload", onHide);
      document.removeEventListener("visibilitychange", onHide);
      persistCurrent();  // flush on unmount (navigating away from Buddy Chat)
    };
  }, [persistCurrent]);

  // G11: on app quit, the main process asks us to persist the active conversation
  // (Electron doesn't reliably fire beforeunload on quit), then we tell it to exit.
  useEffect(() => {
    const off = coworkOfficeOnFlushBeforeQuit(() => {
      try { persistCurrent(); } catch { /* ignore */ }
      // Give the fire-and-forget PUT a beat to leave the socket, then release quit.
      setTimeout(() => { coworkOfficeFlushDone(); }, 250);
    });
    return off;
  }, [persistCurrent]);

  // Fetch the full model catalog + this user's governance-allowed models — the
  // EXACT same two endpoints Chat.jsx uses, so Buddy's picker always mirrors
  // Chat's (Claude, OpenAI, Google/Gemini, Local/in-house), instead of a
  // separate hand-maintained Claude-only list. `models` (derived above) reacts
  // to these two state updates automatically.
  // Extracted so it can be called both on mount AND right before the model
  // dropdown opens (see the <select>'s onFocus below) — otherwise a model an
  // admin adds/syncs via the "LLM Providers" screen while this panel is
  // already open never appears until a full page reload.
  const refreshModelLists = useCallback(() => {
    let alive = true;
    authFetch(`${API_BASE}/all-models`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d?.providers?.length) setAllModelProviders(d.providers); })
      .catch(() => { /* offline / not authorized — keep BASE_MODELS fallback */ });

    // governance_loaded=true means the backend evaluated rules — even if
    // models=[] (everything blocked). We must still flip governanceLoaded so
    // the picker hides blocked models instead of failing open forever.
    authFetch(`${API_BASE}/model-governance/my-models`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!alive || !d?.governance_loaded) return;
        setAllowedModels(d.models || []);
        setGovernanceLoaded(true);
      })
      .catch(() => { /* network error → governanceLoaded stays false → fail-open */ });

    return () => { alive = false; };
  }, []);

  useEffect(() => refreshModelLists(), [refreshModelLists]);

  // Fetch the ops-configurable model lock policy (gateway env). Applies the forced
  // model when locked; re-enables the picker when the deployment sets locked=false.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await authFetch(`${API_BASE}/buddy/model-config`);
        if (!r.ok) return;
        const cfg = await r.json();
        if (!alive) return;
        const locked = !!cfg.locked;
        const forced = cfg.forced_model || DEFAULT_LOCKED_MODEL;
        setModelLocked(locked);
        setLockedModel(forced);
        // Always apply forced_model as the starting model — whether locked or not.
        // When locked=false the picker is shown but the server's preferred model
        // is still the right default. Without this, the UI keeps DEFAULT_LOCKED_MODEL
        // ("claude-opus-4-8") as the active model while the spawned session may
        // report a different model after its first turn, causing the activeModel
        // effect to call setModel() mid-session and trigger a "Switching model" respawn.
        setModel(forced);
        // Also sync the already-spawned CLI session — the session was created
        // before model-config resolved (using the DEFAULT_LOCKED_MODEL default).
        // Without this, run() sends model="claude-sonnet-4-6" but _currentModel
        // is still "claude-opus-4-8" → triggers an unnecessary respawn + notice.
        if (chatIdRef.current) coworkOfficeSetModel(chatIdRef.current, forced);
      } catch { /* keep fallback defaults */ }
    })();
    return () => { alive = false; };
  }, []);

  // Fetch the admin-configurable Buddy prompt queue limit (BUDDY_QUEUE_MAX_WAIT env var).
  // Called once on mount; silently falls back to the default (5) on any error.
  useEffect(() => {
    let alive = true;
    authFetch(`${API_BASE}/buddy/queue-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d?.max_wait != null) setMaxWait(d.max_wait); })
      .catch(() => { /* keep default */ });
    return () => { alive = false; };
  }, []);

  // Switching folder = switching workspace bucket: persist the current conversation,
  // clear state, then EXPLICITLY spawn a session for the new folder (relying on the
  // pre-warm effect alone was racy — it read a stale chatId and left the pane dead).
  // The leaving session is kept alive so returning to it reattaches.
  useEffect(() => {
    if (openingRef.current) { openingRef.current = false; return; }
    persistCurrent();
    // Do NOT interrupt the session we're leaving — keep it alive so switching back
    // to a conversation reattaches (running tasks keep going). See newChat/openConversation.
    refreshConversations();
    setChatId(null); setConvId(null);
    // Spawn a session bound to the new folder so the pane is immediately usable.
    if (isCoworkOfficeAvailable && auth.authenticated) {
      // ensureChatSession early-returns on the current chatId; since we just
      // cleared it via ref below, force a fresh create bound to the new folder.
      chatIdRef.current = null;
      ensureChatSession();
    }
  }, [folder]); // eslint-disable-line react-hooks/exhaustive-deps

  // Load the optional folder's file list (relative paths) for @-mention completion.
  useEffect(() => {
    if (!folder) { setFiles([]); return; }
    let cancelled = false;
    listFolder(folder, { maxFiles: 4000 }).then((list) => {
      if (cancelled) return;
      const rel = (list || []).map((f) => f.path.startsWith(folder)
        ? f.path.slice(folder.length).replace(/^[\\/]/, "")
        : f.name);
      setFiles(rel);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [folder]);

  // Subscribe to the office CLI event stream once.
  useEffect(() => {
    if (!isCoworkOfficeAvailable) return;
    const off = coworkOfficeOnEvent(({ id, event }) => {
      if (event.type === "session:id") {
        // Capture the agent's REAL session_id so this conversation can be --resumed
        // across navigation + app restart. `id` is the live session (chatId); convId
        // is the durable id we key persistence on (may be null for a pre-warmed one).
        const agentSid = event.sessionId;
        const cid = convIdRef.current;
        if (agentSid && sessionMapRef.current[cid]?.resumeId !== agentSid) {
          resumeMapRef.current[cid || id] = agentSid;
          if (cid) sessionMapRef.current[cid] = { chatId: id, resumeId: agentSid };
          // Stamp resumeId onto the conv; the existing persist paths (settle effect,
          // 20s autosave, unmount flush) write it out — no extra save needed here.
          dispatch({ type: "EVENT", id, event: { type: "resume:id", resumeId: agentSid } });
        }
        return;
      }
      // CLI-missing detection: coworkSession._spawn() emits {type:"error", …} then
      // {type:"session:exit", code:-1} when the CLI binary can't be resolved. For a
      // pending DOCUMENT request, swallow the hard-fail and re-route via REST
      // (POST /docs/generate). Non-doc turns fall through to the normal error path.
      if (event.type === "session:exit" && event.code === -1) {
        const pending = pendingDocReqRef.current[id];
        if (pending) {
          delete pendingDocReqRef.current[id];
          submitDocFallbackRef.current?.(id, pending.text, pending.det);
          return; // suppress the raw session:exit so the pane doesn't show "exited"
        }
      }
      // The CLI-not-found error precedes the exit; suppress it only when we're
      // about to run the REST fallback for this session.
      if (event.type === "error" && /CLI binary not found/i.test(event.msg || "") && pendingDocReqRef.current[id]) {
        return;
      }
      dispatch({ type: "EVENT", id, event });

      // Auto-dequeue is handled by a useEffect that watches convStatus (below).
      // Triggering it here (on the raw "result" event) caused a race: the result
      // event arrives from the gateway before the CLI process has fully exited its
      // turn, so calling coworkOfficeRun immediately produced "Session is busy —
      // wait for current turn to finish". The useEffect fires only after React has
      // re-rendered with conv.status === "done", which is the reliable signal that
      // the CLI session is truly idle.
    });
    return off;
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the model dropdown honest: reflect the model the agent ACTUALLY ran
  // (reported via session:init / result → conv.model), so the selector never
  // disagrees with the per-message model badge.
  const activeModel = state.convs[chatId]?.model;
  useEffect(() => {
    if (modelLocked) return;  // keep the pinned model; ignore agent-reported model
    if (activeModel && activeModel !== model) setModel(activeModel);
  }, [activeModel]); // eslint-disable-line react-hooks/exhaustive-deps

  // Persist the active conversation whenever it settles.
  const convStatus = state.convs[chatId]?.status;
  const convLen = state.convs[chatId]?.messages?.length;
  useEffect(() => {
    if (convStatus && convStatus !== "running") { persistCurrent(); refreshConversations(); }
  }, [chatId, convId, folder, convStatus, convLen, persistCurrent, refreshConversations]);

  // Auto-dequeue: fire the next queued prompt once the session is truly idle.
  // We watch convStatus — it transitions to "done" or "idle" only after React
  // has processed the result event AND re-rendered, which is the reliable signal
  // that the CLI session has fully exited its turn. Triggering dequeue on the raw
  // "result" event (the previous approach) caused a race where coworkOfficeRun
  // was called while the CLI was still winding down, producing "Session is busy".
  useEffect(() => {
    if (convStatus !== "done" && convStatus !== "idle") return;
    const next = dequeueNext();
    if (!next) return;
    setQueuedCount((c) => Math.max(0, c - 1));
    // Small tick so the settled "done" render is visible before the next turn starts.
    setTimeout(() => sendChatRef.current?.(next.text, next.attachments ?? []), 50);
  }, [convStatus]); // eslint-disable-line react-hooks/exhaustive-deps

  const newChat = useCallback(() => {
    persistCurrent();
    // Do NOT interrupt the session we're leaving — keep it alive in the manager so
    // returning to it REATTACHES (a running task keeps going in the background, and
    // an idle one is instantly resumable). Sessions are still disposed on the ESC
    // kill-switch and on app quit (disposeAll). This is the core of the session-
    // persistence fix: navigation must not kill the agent.
    refreshConversations();
    setChatId(null); setConvId(null);
    // User explicitly chose a blank new chat — don't auto-restore the prior conv on
    // the next launch (and mark restore done so it can't re-fire this session).
    try { localStorage.removeItem(LAST_CONV_KEY); } catch { /* ignore */ }
    _restoredRef.current = true;
  }, [persistCurrent, refreshConversations]);

  // Open a saved conversation. Three paths, in order of preference:
  //   1. REATTACH — the live session for this conv is still in the manager (we
  //      only navigated away). Just point the UI back at it; a running task and its
  //      tool calls are still going and its events kept updating state.convs.
  //   2. RESUME  — no live session (app was restarted), but we have the agent's
  //      session_id → create with { resumeId } so the agent reloads prior context.
  //   3. FRESH   — no resume id available → new session showing saved messages.
  const openConversation = useCallback(async (c) => {
    if (c.id === convIdRef.current) return;

    // Snapshot the OUTGOING conversation's identity BEFORE any async gap.
    // convIdRef/chatIdRef are still valid here (synchronous); after the first
    // await they may be stale relative to what convsRef holds.
    const _outConvId = convIdRef.current;
    const _outChatId = chatIdRef.current;
    const _outFolder = folderRef.current;

    // Persist the outgoing conversation using the snapshotted ids — NOT via
    // persistCurrent() (which reads refs that will be stale after the awaits).
    if (_outConvId && _outChatId) {
      const _outConv = convsRef.current[_outChatId];
      if (_outConv && _outConv.messages?.length) {
        const _outFirstUser = _outConv.messages.find((m) => m.role === "user");
        const _outTitle = cleanConvTitle(_outFirstUser?.blocks?.[0]?.text || "Conversation");
        convSave({ id: _outConvId, title: _outTitle, messages: _outConv.messages,
                   projectId: _outConv.projectId || "", folder: _outFolder,
                   resumeId: _outConv.resumeId || resumeMapRef.current[_outConvId] || null });
      }
    }

    // (1) Reattach to a still-live session for this conversation.
    const mapped = sessionMapRef.current[c.id];
    if (mapped?.chatId) {
      const live = convsRef.current[mapped.chatId];
      if (live && live.status !== "exited") {
        setChatId(mapped.chatId);
        setConvId(c.id);
        return; // no new process — the background task keeps running (nothing to refresh)
      }
    }

    // Leaving the previous session ALIVE (no interrupt) so it too can be reattached.
    const f = c.folder || folderRef.current || null;

    // Raise the switching flag BEFORE the first await so persistCurrent() (fired by
    // the convStatus effect or the 20s autosave) cannot run with stale refs during
    // the async gap and overwrite the outgoing conversation's data.
    _switchingConvRef.current = true;
    let saved, messages, resumeId, res;
    try {
      // ── FAST PATH: show messages immediately ──────────────────────────────
      // Fetch messages and show them RIGHT AWAY — before the slow session-create
      // (token validation + MCP init + CLI handshake). The user sees their history
      // instantly; the session warms up in the background.
      saved = await convGetFull(c.id);   // fetch messages + resume_id from server
      messages = _sanitize((saved && saved.messages) || []);
      resumeId = (saved && saved.resume_id) || resumeMapRef.current[c.id] || null;

      // Use a stable placeholder chatId so we can render messages immediately
      // while the real session is being created. Keyed on the conv id so it's
      // stable across re-renders during the async gap.
      const _placeholderId = `__placeholder_${c.id}`;
      dispatch({ type: "ADD", conv: {
        id: _placeholderId, kind: "chat", cwd: f, projectId: c.projectId || "",
        title: c.title, status: "idle", statusLine: "", messages, resumeId, pendingConfirm: null,
      }});
      setChatId(_placeholderId);
      setConvId(c.id);
      // Update refs immediately so persistCurrent() and other effects see the
      // new conv id right away (even before the real session is ready).
      convIdRef.current = c.id;
      chatIdRef.current = _placeholderId;
      if (f !== folderRef.current) { openingRef.current = true; folderRef.current = f; setFolder(f); }

      // ── SLOW PATH: create the real session in the background ─────────────
      // Apply the locked/selected model — pass it at CREATE time (matters for ACP,
      // where --model is spawn-time only) and also via setModel() below for the
      // streamjson path where it's just remembered for the next turn.
      const _openModel = modelLocked ? lockedModel : model;
      // (2)/(3) Create — with resumeId when we have one so the agent continues context.
      // Pass c.id as convId so the CLI picks up x-ainxt-conv-id at spawn time
      // (config.toml is read once at startup by both old and new persistent CLIs).
      res = await coworkOfficeCreateSession(f, roleRef.current, projectRef.current, resumeId, _openModel, c.id);
      if (res?.error === "auth_required" && await silentAdopt()) {
        res = await coworkOfficeCreateSession(f, roleRef.current, projectRef.current, resumeId, _openModel, c.id);
      }
      if (res?.error === "auth_required") { setAuth({ authenticated: false, error: "expired" }); return; }
      if (res?.error === "too_many_sessions") {
        uiToast.error(res.message || "Too many tasks are running — wait for one to finish or stop it, then try again.");
        return;
      }
      if (!res?.id) return;

      // Upgrade: replace the placeholder with the real session, carrying over
      // the messages already shown so there's no flicker.
      const _openModel2 = modelLocked ? lockedModel : model;
      if (_openModel2) coworkOfficeSetModel(res.id, _openModel2);
      dispatch({ type: "ADD", conv: { id: res.id, kind: "chat", cwd: f, projectId: c.projectId || "", title: c.title, status: "idle", statusLine: "", messages, resumeId, pendingConfirm: null } });
      // Link this conversation to the (new) live session so a later return reattaches.
      sessionMapRef.current[c.id] = { chatId: res.id, resumeId };
      setChatId(res.id);
      chatIdRef.current = res.id;
      refreshConversations();
    } finally {
      // Always clear the switching flag so persistCurrent() resumes normally.
      _switchingConvRef.current = false;
    }
  }, [persistCurrent, refreshConversations, model, modelLocked, lockedModel]);
  // Bridge for the mount-restore effect (which runs before this is defined).
  useEffect(() => { openConversationRef.current = openConversation; }, [openConversation]);

  const deleteConversation = useCallback((c, e) => {
    if (e) e.stopPropagation();
    convDelete(c.id).then(refreshConversations);
    // Don't let a deleted conversation be restored on next launch.
    try { if (localStorage.getItem(LAST_CONV_KEY) === c.id) localStorage.removeItem(LAST_CONV_KEY); } catch { /* ignore */ }
    if (c.id === convIdRef.current) { setChatId(null); setConvId(null); }
  }, [refreshConversations]);

  // Reuse the EXISTING web-app session for local office mode — no second sign-in.
  // The renderer is already authenticated via the httpOnly session cookie (which
  // JS can't read). We provision the CLI with a LONG-LIVED API KEY (no sid, no
  // expiry) so Buddy never hits the session-registry 401 ("CLI login failure")
  // and stays signed in across restarts (silent re-login).
  //
  // We only mint a NEW key if the desktop doesn't already hold a working one
  // (encrypted in safeStorage) — avoids burning the per-user key cap on every
  // mount. Silent: any failure falls through to the manual "Sign in" path.
  // Returns a STRUCTURED result — { ok, reason, status?, detail? } — rather than a
  // bare boolean so the caller (handleLogin) can surface a user-meaningful message
  // when the silent path fails, instead of dumping raw CLI stderr. Reasons:
  //   "already"        already held a valid key (ok:true)
  //   "minted"         freshly minted + adopted (ok:true)
  //   "mint_failed"    POST /profile/api-keys returned non-2xx (status carried)
  //   "no_key"         gateway returned 2xx but no key in the body
  //   "adopt_failed"   key obtained but main-process validation/write failed
  //   "exception"      network/other error
  const silentAdopt = useCallback(async () => {
    try {
      // 1. Reuse an existing valid desktop key if present.
      const cur = await coworkOfficeHasValidKey();
      if (cur?.valid) return { ok: true, reason: "already" };
      // 2. Otherwise mint a fresh API key from the web session (cookie-authed).
      const label = `desktop:${(typeof navigator !== "undefined" && navigator.platform) || "app"}`;
      const mint = async () => authFetch(`${API_BASE}/profile/api-keys`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label }),
      });
      let r = await mint();
      if (!r.ok) {
        // Retry with exponential backoff — on first mount the httpOnly session
        // cookie may not be fully established yet (timing race on fresh install
        // or after config.json deletion). A fixed 800 ms was too short on slow
        // networks and wasteful on fast ones; we now poll up to 5 times with
        // increasing delays (300 → 600 → 1200 → 2400 → 3000 ms) capped at 10 s
        // total, stopping as soon as the mint succeeds.
        const delays = [300, 600, 1200, 2400, 3000];
        const deadline = Date.now() + 10_000;
        for (const delay of delays) {
          if (Date.now() + delay > deadline) break;
          await new Promise((res) => setTimeout(res, delay));
          r = await mint();
          if (r.ok) break;
        }
      }
      if (!r.ok) {
        let detail = "";
        try { detail = (await r.json())?.detail || ""; } catch { /* non-JSON body */ }
        return { ok: false, reason: "mint_failed", status: r.status, detail };
      }
      const { key } = await r.json();          // raw key, shown once
      if (!key) return { ok: false, reason: "no_key" };
      const res = await coworkOfficeAdoptToken(key, /* isApiKey */ true);
      if (!res?.ok) return { ok: false, reason: "adopt_failed", detail: res?.reason || "" };
      return { ok: true, reason: "minted" };
    } catch (e) {
      return { ok: false, reason: "exception", detail: (e && e.message) || String(e) };
    }
  }, []);

  // Map a silentAdopt failure to a message a NON-technical user can act on.
  // Kept out of render so both handleLogin and the effect can reuse it.
  const adoptErrorMessage = useCallback((res) => {
    if (!res || res.ok) return "";
    switch (res.reason) {
      case "mint_failed":
        if (res.status === 401 || res.status === 403)
          return "We couldn't sign you in automatically — your session may have expired. Use the button below to sign in, or paste your API key directly.";
        if (res.status === 429)
          return "You've reached the maximum number of API keys for your account. Remove an unused key in Profile → API Keys, then try again.";
        return "We couldn't connect your account automatically. Please try signing in using the button below.";
      case "no_key":
        return "We couldn't connect your account automatically. Please sign in using the button below.";
      case "adopt_failed":
        return "We couldn't verify your credentials. Please try signing in again, or paste your API key directly.";
      case "exception":
        return "We couldn't reach the server. Please check your network connection and try again.";
      default:
        return "We couldn't sign you in automatically. Please use the button below to sign in.";
    }
  }, []);

  // Auth check on mount. ALWAYS adopt the current web session first: config.json
  // may hold a stale/synthetic token that merely LOOKS present (authState only
  // checks presence, not validity) or even a PREVIOUS user's token — either would
  // spawn the agent with the wrong identity and read the wrong person's memory.
  // Re-minting from the cookie guarantees the session uses the current user, so
  // memory/connectors/usage are all correct from the very first task.
  useEffect(() => {
    if (!isCoworkOfficeAvailable) { setAuth({ authenticated: false, error: "not_desktop" }); return; }
    (async () => {
      try {
        // Trust silentAdopt directly: if the stored key validates against the gateway,
        // the user IS authenticated — no need to re-read config.json (which may be
        // stale or missing on first launch). Only fall back to the file-read if
        // silentAdopt could not confirm a valid key (e.g. network hiccup, no key stored).
        const adopted = await silentAdopt();
        if (adopted.ok) { setAuth({ authenticated: true }); return; }
        const st = await coworkOfficeAuthState();
        setAuth(st || { authenticated: false, error: "error" });
      } catch { setAuth({ authenticated: false, error: "error" }); }
    })();
  }, [silentAdopt]);

  // Listen for the main-process push that fires when silentRelogin() succeeds
  // asynchronously on app launch (Entra refresh token → fresh API key). Without
  // this the component never learns auth succeeded and stays on the login screen
  // even though the key was minted — because the mount-time silentAdopt() ran
  // before the key was ready and returned false.
  useEffect(() => {
    return coworkOfficeOnAuthUpdated(({ authenticated }) => {
      if (authenticated) setAuth({ authenticated: true });
    });
  }, []);

  // Auto-scroll on ANY content change (streamed tokens, tool calls, status) — the
  // conv object is cloned on every event, so depending on it fires each update.
  // Only scroll if the user is at the bottom (stick-to-bottom); never yank them
  // down while they're reading scrollback.
  useEffect(() => {
    if (atBottomRef.current) {
      chatScrollRef.current?.scrollTo({ top: chatScrollRef.current.scrollHeight, behavior: "smooth" });
    }
  }, [chatId, state.convs[chatId]]);

  // On switching conversations, snap to the bottom (instant) and reset stickiness.
  useEffect(() => { scrollToBottom("auto"); }, [chatId, scrollToBottom]);

  useEffect(() => { compItemRef.current?.scrollIntoView({ block: "nearest" }); }, [compIdx]);

  const handleLogin = useCallback(async () => {
    setLoggingIn(true); setLoginLog(""); setAdoptError("");

    // Fast path: reuse the existing web-app session (cookie-authed API key mint).
    // This works whenever the user is already signed into the desktop app — no CLI
    // binary needed, no browser prompt, no device code. Only fall through to the
    // full `ainxt login` subprocess if this fails (e.g. session truly expired).
    // IMPORTANT: trust silentAdopt directly — do NOT re-read authState from
    // config.json. On a fresh install config.json may not exist yet, so
    // readAuthState() returns authenticated:false even after a successful key mint.
    // adopt-token already validated the key against the gateway — if it returned
    // ok:true the user IS authenticated, no second file-read needed.
    try {
      const adopted = await silentAdopt();
      if (adopted.ok) {
        setAuth({ authenticated: true });
        setLoggingIn(false);
        return;
      }
      // Explain WHY the silent path failed in plain language before falling back.
      setAdoptError(adoptErrorMessage(adopted));
    } catch { /* fall through to manual login */ }

    // Slow path: spawn `ainxt login` (device-code / browser flow).
    // Surface a clear message if the CLI binary is missing so the user does not
    // see a cryptic timeout — the login-output stream will carry the error text.
    const off = coworkOfficeOnLoginOutput(({ text }) => {
      if (!text) return;
      setLoginLog((s) => (s + text).slice(-4000));
    });

    // Fix #11: after a session expiry the login subprocess can hang waiting on an
    // interactive prompt that never receives input, so coworkOfficeLogin() never
    // resolves and the button is stuck on "Signing in…". Race it against a timeout
    // so the UI ALWAYS recovers and the user can retry, instead of freezing.
    //
    // Additionally, POLL silentAdopt() every 2 s while the browser is open. Once
    // the user completes the web-portal login the session cookie is set and we can
    // mint an API key immediately — without waiting for the CLI device-code flow
    // to complete. This fixes the "spinner keeps rotating" issue where the browser
    // opens the portal, the user logs in, but `ainxt login` never detects it.
    const LOGIN_TIMEOUT_MS = 120000;
    let timer;
    const timeout = new Promise((resolve) => {
      timer = setTimeout(() => resolve({ authenticated: false, error: "timeout" }), LOGIN_TIMEOUT_MS);
    });

    const POLL_INTERVAL_MS = 2000;
    const POLL_DEADLINE = Date.now() + LOGIN_TIMEOUT_MS;
    let pollTimer = null;
    let pollResolved = false;

    const pollAdopt = async () => {
      if (pollResolved || Date.now() > POLL_DEADLINE) return;
      const r = await silentAdopt();
      if (r.ok) {
        pollResolved = true;
        clearTimeout(timer);
        clearTimeout(pollTimer);
        off();
        setAuth({ authenticated: true });
        setLoggingIn(false);
        return;
      }
      pollTimer = setTimeout(pollAdopt, POLL_INTERVAL_MS);
    };
    pollTimer = setTimeout(pollAdopt, POLL_INTERVAL_MS);

    let next;
    try {
      next = await Promise.race([coworkOfficeLogin(), timeout]);
    } catch (e) {
      next = { authenticated: false, error: String(e?.message || e || "login_failed") };
    } finally {
      clearTimeout(timer);
      clearTimeout(pollTimer);
      off();
    }

    if (pollResolved) return; // polling already authenticated — don't overwrite

    if (next?.error === "timeout") {
      // G8: actually KILL the hung login subprocess (main-process handle) so it
      // doesn't leak or block the next attempt — not just resolve the Promise.
      try { await coworkOfficeCancelLogin(); } catch { /* ignore */ }
      setLoginLog((s) => s +
        "\n\nSign-in timed out and was cancelled.\n" +
        "Possible causes:\n" +
        "  • AiNxt CLI binary not found in the app bundle (contact your admin)\n" +
        "  • Browser/device-code flow was not completed within 2 minutes\n" +
        "  • Corporate VPN or proxy blocked the authentication callback\n" +
        "Please try again, or ask your admin to verify the CLI binary is bundled.");
    }
    setAuth(next || { authenticated: false, error: "login_failed" });
    setLoggingIn(false);
  }, [silentAdopt, adoptErrorMessage]);

  // Parse the loginLog for a device-code URL so we can surface it as a button.
  useEffect(() => {
    if (!loginLog) { setLoginUrl(""); return; }
    const m = loginLog.match(/https?:\/\/\S+/);
    setLoginUrl(m ? m[0].replace(/[.,;)]+$/, "") : "");
  }, [loginLog]);

  // Handle manual API key submission.
  const handleManualKey = useCallback(async () => {
    const key = manualKey.trim();
    if (!key) return;
    setManualKeyError("");
    setLoggingIn(true);
    try {
      const res = await coworkOfficeAdoptToken(key, /* isApiKey */ true);
      if (res?.ok) {
        setAuth({ authenticated: true });
        setManualKey("");
      } else {
        setManualKeyError("That API key couldn't be verified. Please check it and try again.");
      }
    } catch {
      setManualKeyError("Something went wrong. Please check your connection and try again.");
    } finally {
      setLoggingIn(false);
    }
  }, [manualKey]);

  const chooseFolder = useCallback(async () => {
    const f = await pickFolder();
    if (f) setFolder(f);
  }, []);

  // Attach file(s) to the chat. Uses a hidden <input type="file"> in the browser
  // to get real File objects (with bytes in memory). In Electron, File objects
  // have a .path property (absolute disk path) so we get both:
  //   - bytes → upload to /chat/upload (server parses: python-docx, pdfplumber, etc.)
  //   - path  → passed to agent as reference for extract_document tool
  //
  // This approach works on ALL exe versions because it uses the browser's native
  // file API — no IPC needed for reading. The server does all parsing, identical
  // to how web Chat handles attachments.
  //
  // Tier order:
  //   TIER 0: readFileSpreadsheet IPC (xlsx/xls/xlsm — SheetJS, all exe versions)
  //   TIER 1: server upload via File object bytes → /chat/upload (ALL exe versions)
  //   TIER 2: readFile IPC → _extractAny() (new exe builds only, 25 MB limit)
  //   TIER 3: readFileBinary IPC → server upload (new exe builds only)

  // Shared upload helper — used by both the paperclip handler and the auto-upload
  // in handleSend. POSTs a File/Blob to /chat/upload (same endpoint as web Chat).
  //
  // Uses XMLHttpRequest (not fetch) so we can report real upload-byte progress —
  // fetch() exposes no upload-progress events. Mirrors the pattern already used
  // by Chat.jsx / Office.jsx for the same endpoint. onProgress is optional;
  // callers that don't care about progress (e.g. folder auto-upload) can omit it.
  const uploadFileToServer = useCallback(async (file, name, onProgress) => {
    const uploadUrl = `${API_BASE}/chat/upload`;
    const fd = new FormData();
    fd.append("files", file, name);

    const data = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", uploadUrl);
      xhr.withCredentials = true; // sends httpOnly auth_token cookie
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable && onProgress) {
          onProgress(Math.round((ev.loaded / ev.total) * 100));
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try { resolve(JSON.parse(xhr.responseText)); }
          catch { reject(new Error("Invalid response")); }
        } else {
          reject(new Error(`Server upload HTTP ${xhr.status}: ${xhr.responseText || "(unreadable)"}`));
        }
      };
      xhr.onerror = () => reject(new Error("Network error"));
      xhr.send(fd);
    });
    if (onProgress) onProgress(100);
    const all = data.uploaded || [];
    const uploaded = all.find((u) => !u.blocked);
    // Surface a compliance/DLP block instead of silently returning a null id — the
    // caller needs to tell the user WHY a file could not be attached.
    const blocked = !uploaded ? all.find((u) => u.blocked) : null;
    return {
      serverId: uploaded?.id || null,
      parsedText: uploaded?.parsed_text || null,
      blocked: !!blocked,
      blockReason: blocked?.block_reason || (blocked ? "blocked by content policy" : null),
    };
  }, []);

  const attachFile = useCallback(() => {
    // Trigger the hidden file input — the actual processing happens in
    // handleFileInputChange when the user selects files.
    if (fileInputRef.current) {
      fileInputRef.current.value = "";   // reset so same file can be re-selected
      fileInputRef.current.click();
    }
  }, []);

  // Called when the hidden file input fires onChange (user selected files).
  const handleFileInputChange = useCallback(async (e) => {
    const rawFileList = Array.from(e.target.files || []);
    if (!rawFileList.length) return;

    // ── Hard guard: image files cannot be attached via Buddy's paperclip
    // button. The <input accept=…> filter already hides image extensions
    // from the OS file picker, but a user can still bypass that by choosing
    // "All Files" — so we re-check here regardless of what the picker allowed
    // through. Only strips images; any other files in the same selection are
    // still attached normally.
    const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp"]);
    const fileList = rawFileList.filter((f) => {
      const ext = (f.name.split(".").pop() || "").toLowerCase();
      return !IMAGE_EXTS.has(ext);
    });
    const hadImages = fileList.length !== rawFileList.length;
    if (hadImages) setImageBlockedError(true);
    if (!fileList.length) {
      if (e.target?.value !== undefined) e.target.value = "";
      return;
    }

    const MAX_FILES = 5;
    if (attachedFiles.length + fileList.length > MAX_FILES) {
      setFileLimitError(true);
      if (e.target?.value !== undefined) e.target.value = "";
      return;
    }

    setAttaching(true);

    // Set folder from the first file's path (Electron File objects have .path)
    const firstPath = fileList[0].path;
    if (firstPath) {
      const dir = String(firstPath).replace(/[\/\\][^\/\\]*$/, "") || null;
      if (dir) { folderRef.current = dir; setFolder(dir); }
    }

    // Detect which IPC methods the installed exe exposes.
    const hasBinaryIpc      = typeof window?.ainxtDesktop?.readFileBinary      === "function";
    const hasSpreadsheetIpc = typeof window?.ainxtDesktop?.readFileSpreadsheet === "function";

    // uploadFileToServer is now a shared useCallback defined above — used here
    // and also in handleSend for auto-uploading folder files.

    // Helper: convert base64 string → Blob for IPC-based server upload
    const base64ToBlob = (b64, mime) => {
      const binary = atob(b64);
      const bytes  = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      return new Blob([bytes], { type: mime });
    };

    const MIME_MAP = {
      xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      xls:  "application/vnd.ms-excel",
      xlsm: "application/vnd.ms-excel.sheet.macroEnabled.12",
      docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      pdf:  "application/pdf",
      odt:  "application/vnd.oasis.opendocument.text",
      ods:  "application/vnd.oasis.opendocument.spreadsheet",
      ppt:  "application/vnd.ms-powerpoint",
      rtf:  "application/rtf",
      html: "text/html", htm: "text/html",
      png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
      gif: "image/gif", webp: "image/webp", bmp: "image/bmp",
    };

    // Reset per-file progress for the newly selected batch — each file starts
    // at 0 and is updated live as its network upload (if any) makes progress.
    setAttachProgress(Object.fromEntries(fileList.map((f) => [f.name, 0])));

    const items = await Promise.all(fileList.map(async (file) => {
      const name = file.name;
      const ext  = (name.split(".").pop() || "").toLowerCase();
      const p    = file.path || null;   // absolute disk path (Electron only)
      let extractedText = null;
      // Extraction warnings (e.g. "sheet has 50,000 rows, only 200,000 shown" —
      // won't actually fire until the sheet is over the raised row cap, but this
      // plumbs the field through regardless) — surfaced explicitly in the prompt
      // instead of relying on the silent "[truncated]" marker inside the text
      // itself, so the model states the data is partial instead of retrying the
      // same extract/read call and tripping the loop guard.
      let extractWarnings = [];
      const onProgress = (pct) => setAttachProgress((prev) => ({ ...prev, [name]: pct }));

      // ── TIER 0: readFileSpreadsheet IPC for xlsx/xls/xlsm ────────────────────
      if (["xlsx", "xls", "xlsm"].includes(ext) && hasSpreadsheetIpc && p) {
        try {
          const result = await readFileSpreadsheet(p);
          if (result && !result.error && result.text) {
            extractedText = result.text;
            extractWarnings = Array.isArray(result.warnings) ? result.warnings : [];
          }
        } catch (_) { /* fall through to next tier */ }
        if (extractedText) {
          // Also upload to server so the file gets a serverId — without it the agent
          // cannot pass attachment_id to outlook_send_mail / teams_send_message and
          // will either refuse to send or try to rebuild the file from scratch.
          let tier0ServerId = null;
          try {
            const up0 = await uploadFileToServer(file, name, onProgress);
            tier0ServerId = up0.serverId;
          } catch (upErr) {
          }
          return { name, path: p, extractedText, serverId: tier0ServerId, extractWarnings };
        }
      }

      // ── TIER 1: server upload via File object bytes ───────────────────────────
      const SERVER_FORMATS = ["docx","xlsx","xls","xlsm","pdf","pptx","odt","ods","ppt","rtf","html","htm","png","jpg","jpeg","gif","webp","bmp","csv","txt","md","json","xml","yaml","yml"];
      let serverId = null;
      if (!extractedText && SERVER_FORMATS.includes(ext)) {
        try {
          const up = await uploadFileToServer(file, name, onProgress);
          extractedText = up.parsedText;
          serverId = up.serverId;
        } catch (_) { /* fall through to next tier */ }
      }

      // ── TIER 2: readFile IPC → main.js _extractAny() ─────────────────────────
      if (!extractedText && p) {
        try {
          const result = await readFile(p);
          if (result && !result.error && result.content) {
            extractedText = result.content;
            extractWarnings = Array.isArray(result.warnings) ? result.warnings : [];
          }
        } catch (_) { /* fall through to next tier */ }
      }

      // ── TIER 3: readFileBinary IPC → server upload (new exe only) ────────────
      if (!extractedText && hasBinaryIpc && p) {
        const serverFormats = ["xlsx","xls","xlsm","docx","pptx","pdf","odt","ods","ppt","rtf","html","htm"];
        if (serverFormats.includes(ext)) {
          try {
            const { base64, error: readErr } = await readFileBinary(p);
            if (!readErr && base64) {
              const mime = MIME_MAP[ext] || "application/octet-stream";
              const blob = base64ToBlob(base64, mime);
              const up3 = await uploadFileToServer(blob, name, onProgress);
              extractedText = up3.parsedText;
              serverId = up3.serverId;
            }
          } catch (_) { /* fall through */ }
        }
      }

      return { name, path: p, extractedText, serverId, extractWarnings };
    }));

    setAttachedFiles(items);
    setAttaching(false);
    setAttachProgress({});
  }, [attachedFiles]);
  const clearAttachment = useCallback((name) => {
    setAttachedFiles((prev) => prev.filter((f) => f.name !== name));
  }, []);

  // Installable plugins / role specialists. ONLY published roles appear in the
  // picker (the governance gate) — drafts are admin-only in Buddy Setup. The
  // selected one's prompt + scoped tools + skills apply when a session spawns.
  useEffect(() => {
    authFetch(`${API_BASE}/buddy/roles?published=1`).then((r) => r.json()).then((d) => setRoles(d?.roles || [])).catch(() => {});
  }, []);

  // Projects are SERVER-persisted now (Postgres), not localStorage — durable +
  // multi-device, and schedules can reference them. Load on mount.
  const loadProjects = useCallback(() => {
    authFetch(`${API_BASE}/buddy/projects`).then((r) => r.json())
      .then((d) => setProjects(d?.projects || [])).catch(() => {});
  }, []);
  useEffect(() => { loadProjects(); }, [loadProjects]);

  // Scheduled-task list + all CRUD/history now live in CoworkScheduler.jsx —
  // it fetches /buddy/tasks itself when opened (scoped to the active project).
  const selectedRole = roles.find((r) => r.id === roleId) || null;
  useEffect(() => {
    roleRef.current = selectedRole
      ? { id: selectedRole.id, name: selectedRole.name, system_prompt: selectedRole.system_prompt, allowed_connectors: selectedRole.allowed_connectors || [] }
      : null;
  }, [roleId, roles]); // eslint-disable-line react-hooks/exhaustive-deps

  // Active project → its instructions + persistent memory inject into new sessions.
  // Sync the ref DURING render (not in a post-render effect) so a synchronously
  // triggered newChat() always sees the current project — the effect ran too
  // late, so sessions were created with the stale/empty project.
  const activeProject = projects.find((p) => p.id === activeProjectId) || null;
  projectRef.current = activeProject
    ? { name: activeProject.name, instructions: activeProject.instructions || "", memory: activeProject.memory || "", folder: activeProject.folder || null }
    : null;
  activeProjectIdRef.current = activeProjectId || "";

  const saveProject = async () => {
    const p = editingProject;
    if (!p || !(p.name || "").trim()) { setEditingProject(null); return; }
    const payload = { name: p.name.trim(), instructions: p.instructions || "", memory: p.memory || "", folder: p.folder || null };
    try {
      const res = p.id
        ? await authFetch(`${API_BASE}/buddy/projects/${p.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
        : await authFetch(`${API_BASE}/buddy/projects`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const data = await res.json().catch(() => ({}));
      const id = p.id || data.id;
      loadProjects();
      setActiveProjectId(id);
      // Apply the project's context (incl. folder = document scope) immediately.
      projectRef.current = { name: payload.name, instructions: payload.instructions, memory: payload.memory, folder: payload.folder };
      activeProjectIdRef.current = id;
      if (payload.folder) { folderRef.current = payload.folder; setFolder(payload.folder); }
    } catch (_) { /* surface nothing — list reload reflects server truth */ }
    setEditingProject(null);
  };
  const removeProject = async (id) => {
    const ok = await confirm({
      title: "Delete project",
      message: "Delete this project? Its scheduled tasks are kept (just unlinked).",
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!ok) return;
    try { await authFetch(`${API_BASE}/buddy/projects/${id}`, { method: "DELETE" }); } catch (_) { /* ignore */ }
    loadProjects();
    if (activeProjectId === id) setActiveProjectId("");
  };

  // ── Durable memory (server-side, scoped to JWT sub) ──────────────────────────
  // What Buddy remembers about YOU across every task: editable prefs + the facts
  // the agent saved via its `remember` tool. Transparency + control = Buddy parity.
  const openMemory = async () => {
    setShowMemory(true); setMemNote("");
    try {
      const r = await authFetch(`${API_BASE}/buddy/prefs`);
      const d = r.ok ? await r.json() : {};
      setMemPrefs(d?.prefs || {});
    } catch (_) { setMemPrefs({}); }
  };
  const addMemNote = async () => {
    const note = memNote.trim();
    if (!note || memBusy) return;
    setMemBusy(true);
    try {
      const r = await authFetch(`${API_BASE}/buddy/memory/note`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { setToast(d?.detail || "Couldn't save that note."); }
      else { setMemPrefs(d.prefs || {}); setMemNote(""); }
    } catch (e) { setToast(String(e?.message || e)); }
    setMemBusy(false);
  };
  const delMemNote = async (note) => {
    try {
      const r = await authFetch(`${API_BASE}/buddy/memory/note?note=${encodeURIComponent(note)}`, { method: "DELETE" });
      const d = await r.json().catch(() => ({}));
      if (r.ok) setMemPrefs(d.prefs || {});
    } catch (_) { /* list reload reflects server truth */ }
  };
  // Persist a single preference key (debounced-by-blur via the caller).
  const saveMemPref = async (key, value) => {
    try {
      const next = { ...(memPrefs || {}), [key]: value };
      setMemPrefs(next);
      await authFetch(`${API_BASE}/buddy/prefs`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prefs: { [key]: value } }),
      });
    } catch (_) { /* ignore — next open re-reads server truth */ }
  };

  // A session can start WITHOUT a folder (office work via connectors/docs). When a
  // folder is present the agent may read/write there too.
  const ensureChatSession = useCallback(async () => {
    // If the current chatId is a placeholder (set during openConversation's fast-path
    // while the real session is still being created), don't return it — fall through
    // to create a real session. The placeholder is replaced by the real session id
    // once coworkOfficeCreateSession resolves.
    if (chatId && !chatId.startsWith("__placeholder_") && state.convs[chatId] && state.convs[chatId].status !== "exited") return chatId;
    // A Project's folder is its document scope: when a project with a folder is
    // active, the session is confined to it; otherwise the (optional) ad-hoc folder.
    // An explicitly-attached folder WINS over the project's default folder, so
    // attaching a folder always takes effect (project folder is just the default).
    const cwd = folder || (projectRef.current && projectRef.current.folder) || null;
    // G1: if THIS conversation had a live agent session that was evicted by the pool
    // (or exited), rehydrate it with --resume instead of spawning a blank agent that
    // has forgotten the task. resumeId is keyed by the durable conversation id.
    const _cid = convIdRef.current;
    const _resumeId = (_cid && (resumeMapRef.current[_cid]
                      || sessionMapRef.current[_cid]?.resumeId)) || null;
    // Force the locked model on every session so the agent always runs on it.
    // Passed at CREATE time too (matters for ACP, where --model is spawn-time
    // only) as well as via setModel() below for the streamjson path.
    const _sessionModel = modelLocked ? lockedModel : model;
    // Pass convId at create time so the CLI injects x-ainxt-conv-id into
    // config.toml before spawning — both old (streamjson --full) and new (ACP)
    // persistent CLIs read config.toml once at startup, so this is the only
    // reliable way to get the header into every inference request.
    // _cid may be null for a brand-new conversation (first message not sent yet);
    // in that case run() writes it on the first send via _injectConvIdHeader().
    let res = await coworkOfficeCreateSession(cwd, roleRef.current, projectRef.current, _resumeId, _sessionModel, _cid || null);
    if (res?.error === "auth_required" && await silentAdopt()) {
      res = await coworkOfficeCreateSession(cwd, roleRef.current, projectRef.current, _resumeId, _sessionModel, _cid || null);
    }
    if (res?.error === "auth_required") { setAuth({ authenticated: false, error: "expired" }); return null; }
    if (res?.error === "too_many_sessions") {
      // G20: pool at hard cap — tell the user instead of silently failing.
      uiToast.error(res.message || "Too many tasks are running — wait for one to finish or stop it, then try again.");
      return null;
    }
    if (!res?.id) return null;
    // Preserve any existing conversation messages so history isn't lost when a
    // new CLI session is spawned (e.g. after pool eviction or session exit while
    // queued messages are waiting). Without this, the ADD dispatch would reset
    // messages to [] and all prior turns would disappear from the UI.
    const _existingMessages = (chatId && state.convs[chatId]?.messages) || [];
    dispatch({ type: "ADD", conv: { id: res.id, kind: "chat", cwd, projectId: activeProjectIdRef.current || "", title: "Chat", status: "idle", statusLine: "", messages: _existingMessages, resumeId: _resumeId, pendingConfirm: null } });
    // Keep the conv→session map current so a later eviction/return still resumes.
    if (_cid) sessionMapRef.current[_cid] = { chatId: res.id, resumeId: _resumeId };
    if (_sessionModel) coworkOfficeSetModel(res.id, _sessionModel);
    if (permMode && permMode !== "default") coworkOfficeSetPermissionMode(res.id, permMode);
    setChatId(res.id);
    return res.id;
  }, [chatId, state.convs, folder, model, permMode, modelLocked, lockedModel]);

  // Pre-warm a session on mount (and on folder change) so the SDK initialize
  // handshake runs and the "/" command list is ready BEFORE the first message.
  // Unlike Code, no folder is required to boot the office session.
  //
  // G-race: gated on `restoreAttempted` so this never wins the mount-time race
  // against the last-conversation restore above. Without this gate, pre-warm's
  // local auth check resolved before the restore effect's server round trip
  // finished, so it spawned (and claimed chatId with) a brand-new blank session
  // first — silently stranding the user's actual conversation (and anything they
  // typed afterward went into the orphaned blank one instead). Once restore has
  // settled — either it reattached/resumed a real conversation, or it confirmed
  // there was nothing to restore — it's safe to pre-warm a fresh one.
  useEffect(() => {
    if (!isCoworkOfficeAvailable || !auth.authenticated || !restoreAttempted) return;
    // Don't pre-warm when a placeholder is active (openConversation's fast-path
    // already set chatId to a placeholder while the real session is being created).
    if (chatId || openingRef.current) return;
    ensureChatSession();
  }, [folder, chatId, auth.authenticated, restoreAttempted, ensureChatSession]);

  // CLI-missing REST fallback: submit a doc job through the SAME endpoint Chat
  // uses (POST /docs/generate) and, on success, dispatch a synthetic `document`
  // event so the existing DocDownloadButton renders + polls /docs/job/{id}/status.
  // Fail-open: any error surfaces as a notice, matching the original error path.
  const submitDocFallback = useCallback(async (sessionId, text, det) => {
    const conv = convsRef.current[sessionId];
    // Ensure there's an assistant message to attach the doc/notice block to.
    if (conv && conv.status !== "running") {
      dispatch({ type: "EVENT", id: sessionId, event: { type: "agent:ttfb" } });
    }
    dispatch({ type: "EVENT", id: sessionId, event: { type: "notice", level: "warn", msg: "Local Buddy CLI isn't available here — generating your document on the server instead." } });
    try {
      const attachIds = (conv?.messages || [])
        .flatMap((m) => (m.attachments || []).map((a) => a.id))
        .filter(Boolean);
      const body = {
        question:        text,
        format:          det.format,
        doc_intent:      det.intent,
        chat_id:         convIdRef.current || sessionId,
        title:           cleanConvTitle(text).slice(0, 80),
        attachment_ids:  attachIds,
        user_model_hint: model || "auto",
      };
      const r = await authFetch(`${API_BASE}/docs/generate`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      });
      if (!r.ok) throw new Error(`Server error ${r.status}`);
      const d = await r.json();
      if (!d.job_id) throw new Error(d.detail || "Document generation failed");
      const filename = d.filename_hint || `document.${det.format}`;
      dispatch({ type: "EVENT", id: sessionId, event: { type: "document", jobId: d.job_id, format: det.format, filename } });
      dispatch({ type: "EVENT", id: sessionId, event: { type: "result", status: "ok", response: "" } });
    } catch (e) {
      dispatch({ type: "EVENT", id: sessionId, event: { type: "error", msg: `Document generation failed: ${e.message}` } });
      dispatch({ type: "EVENT", id: sessionId, event: { type: "result", status: "error", error: e.message } });
    }
  }, [model]);
  useEffect(() => { submitDocFallbackRef.current = submitDocFallback; }, [submitDocFallback]);

  // Derived from state — must be declared BEFORE sendChat (and any other
  // useCallback that closes over chatBusy) to avoid a TDZ crash. The component
  // renders an early-return guard above, so chatId / state.convs are stable here.
  const chat = chatId ? state.convs[chatId] : null;
  const chatBusy = chat?.status === "running";
  // True when the user has filled all available queue slots. Used to disable the
  // textarea and send button and show a persistent limit-reached notice.
  const isQueueFull = maxWait > 0 && queuedCount >= maxWait;

  const sendChat = useCallback(async (override, overrideAttachments) => {
    const text = (override ?? chatInput).trim();
    if (!text) return;                          // no folder requirement

    // ── Prompt queue: if Buddy is busy and this is a user-initiated send ──────
    // Auto-dequeue calls pass overrideAttachments as an Array (even if empty),
    // so Array.isArray(overrideAttachments) distinguishes them from user sends.
    const isAutoDequeue = Array.isArray(overrideAttachments);
    if (chatBusy && !isAutoDequeue) {
      // User pressed Send/Enter (or clicked a suggestion) while agent is processing.
      const payload = { text, attachments: [...attachedFiles] };
      const accepted = enqueue(payload);
      if (!accepted) {
        uiToast(`Queue full — max ${maxWait} message${maxWait === 1 ? "" : "s"} allowed. Please wait.`);
      } else {
        setQueuedCount((c) => c + 1);
        setChatInput("");
      }
      return;
    }

    // Client-handled command: /model [name] switches the live agent's model.
    if (text === "/model" || text.startsWith("/model ")) {
      if (modelLocked) {
        const id0 = await ensureChatSession();
        if (id0) dispatch({ type: "EVENT", id: id0, event: { type: "notice", level: "warn", msg: `Model is locked to ${MODEL_LABEL(lockedModel)} for now — switching is disabled.` } });
        setChatInput("");
        return;
      }
      const arg = text.slice(6).trim();
      const id0 = await ensureChatSession();
      if (!arg) {
        if (id0) dispatch({ type: "EVENT", id: id0, event: { type: "notice", level: "warn", msg: `Current model: ${MODEL_LABEL_FROM(models, model)}. Switch with: ${models.map((m) => "/model " + m.key).join("  ·  ")}` } });
        setChatInput("");
        return;
      }
      const target = models.find((m) => m.key === arg || m.label.toLowerCase() === arg.toLowerCase())?.key
        || MODEL_ALIASES[arg.toLowerCase()]
        || arg;
      setModel(target);
      if (id0) { coworkOfficeSetModel(id0, target); dispatch({ type: "EVENT", id: id0, event: { type: "notice", level: "warn", msg: `Model switched to ${MODEL_LABEL_FROM(models, target)}.` } }); }
      setChatInput("");
      return;
    }

    // Office command: /schedule [what to do] → open the recurring-task scheduler.
    if (text === "/schedule" || text.startsWith("/schedule ")) {
      setSchedulePrompt(text.slice(9).trim());
      setShowSchedule(true);
      setChatInput("");
      return;
    }

    if (!convIdRef.current) { const cid = crypto.randomUUID(); convIdRef.current = cid; setConvId(cid); }
    const id = await ensureChatSession();
    if (!id) return;
    // Link this conversation to its live session so navigating away + back
    // REATTACHES instead of spawning a fresh empty agent (session persistence).
    sessionMapRef.current[convIdRef.current] = {
      chatId: id, resumeId: resumeMapRef.current[convIdRef.current] || null,
    };
    const prior = state.convs[id]?.messages || [];
    if (override == null) setChatInput("");
    // Persist the attached-file chips ONTO the user message so they stay visible
    // after send (the composer chips are cleared below). Shape: {id, name,
    // serverId}. `id` stays local-path-or-name (unchanged — other code may key
    // off it); `serverId` is the ChatAttachment DB id (null when a file was only
    // extracted locally and never uploaded, e.g. TIER 2) and is what
    // GET /chat/attachments/{id}/raw needs to re-download the file later.
    // Use overrideAttachments when auto-dequeuing (they were captured at enqueue time).
    const effectiveAttachments = overrideAttachments ?? attachedFiles;
    dispatch({ type: "USER_TURN", id, text,
      attachments: effectiveAttachments.map((f) => ({ id: f.path || f.name, name: f.name, serverId: f.serverId || null })) });
    // First message of a live session (sent to the agent only — not shown):
    //  - an office-context preamble so the agent uses connectors + attached
    //    documents (NOT "read a repo"); if a folder is present, mention it as an
    //    optional local working area;
    //  - if this is a REOPENED conversation, replay the prior exchange for continuity.
    let toSend = text;
    const isSlash = text.startsWith("/");
    // Key primedRef on convId:sessionId (not just sessionId) so the office
    // preamble fires again on every new session spawn for this conversation —
    // including after resume failures, model switches, and idle evictions.
    // Previously keyed only on sessionId, so a second respawn within the same
    // conversation would skip the preamble (primedRef already had the old id).
    const _primeKey = `${convIdRef.current || ""}:${id}`;
    if (!isSlash && !primedRef.current.has(_primeKey)) {
      primedRef.current.add(_primeKey);
      let pre = "[You are the AiNxt office assistant working locally. Get office work done using your connectors (Outlook/Teams, Jira, Confluence) and any attached documents — there is no code repository to read, and you have no terminal/shell. When the user wants a Word/Excel/PowerPoint/PDF, produce a downloadable document with your document tool. NEVER send an email, post to Teams, or write to a connector without explicit confirmation — those actions are gated.]";
      if (folder) {
        const folderFileList = files.length
          ? `\n  Files in folder: ${files.slice(0, 30).join(', ')}${files.length > 30 ? ` … and ${files.length - 30} more` : ''}`
          : '';
        pre += `\n\n[An optional local working folder is available at ${folder}; you may read or save files there when relevant.${folderFileList} If the user asks to send, share, email, or post a file from this folder: the system will automatically upload the files and provide attachment_ids — use those attachment_ids with outlook_send_mail and teams_send_chat_message. Do NOT use attachment_file_path or attachment_file_paths — the server cannot access local Windows paths.]`;
      }
      const transcript = prior
        .map((m) => {
          const t = (m.blocks || []).filter((b) => b.kind === "text").map((b) => b.text).join(" ").trim();
          return t ? `${m.role === "user" ? "User" : "Assistant"}: ${t}` : "";
        })
        .filter(Boolean)
        .join("\n\n");
      if (transcript) pre += `\n\n[This conversation continues from earlier — you already did the work below; don't redo it. Prior exchange:\n${transcript}\n]`;
      toSend = `${pre}\n\nMy message:\n${text}`;
    }
    // Attached files: for Office documents (docx/xlsx/pptx) we already extracted
    // text client-side in attachFile() — inject it directly so the agent can read it
    // without needing to open a binary file. For other files (pdf, txt, etc.) we
    // keep the original path-reference approach so the agent can use its Read tool.
    if (effectiveAttachments.length) {
      // Split into files with extracted text vs files the agent must read by path
      const extracted = effectiveAttachments.filter((f) => f.extractedText);
      const byPath    = effectiveAttachments.filter((f) => !f.extractedText);

      // Inject extracted text blocks directly into the message.
      // Also tell the agent the server-side attachment_id so it can pass it to
      // outlook_send_mail / teams_send_message without needing a local file path.
      if (extracted.length) {
        const blocks = extracted
          .map((f) => {
            const idHint = f.serverId
              ? ` [attachment_id=${f.serverId}]`
              : "";
            // No re-truncation — the extractor (attachFile(), and desktop/src/main.js's
            // _extractWorkbook/_extractDocument on the IPC side) already produced this
            // text in full. We used to re-slice it to a flat 20,000 chars "to be safe",
            // which silently cut spreadsheets off after a few dozen rows before the
            // model ever saw the rest. Since responses stream back, there's no need to
            // cap the injected text here.
            // Guard against a non-string extractedText (shouldn't happen, but avoids a
            // hard crash in handleSend if it ever does) instead of assuming a string.
            const text = typeof f.extractedText === "string" ? f.extractedText : String(f.extractedText ?? "");
            // Surface any extraction truncation as a stated fact ahead of the file
            // content, instead of leaving it buried as a "[TRUNCATED: ...]" marker
            // at the tail of the text. This is what stops the model from concluding
            // the read failed and retrying the same extract/read call (the root
            // cause of the "runaway loop" symptom on very large workbooks).
            const warnings = Array.isArray(f.extractWarnings) ? f.extractWarnings : [];
            const warningBlock = warnings.length
              ? `\n⚠️ NOTE — this file was only partially read: ${warnings.join(" ")}\n`
              : "";
            return `[File: ${f.name}${idHint}]${warningBlock}\n${text}`;
          })
          .join("\n\n");
        const sendableIds = extracted.filter((f) => f.serverId).map((f) => f.serverId);
        const sendHint = sendableIds.length
          ? sendableIds.length === 1
            ? `\n\n[This uploaded file is stored on the server. To send/share it via Outlook or Teams, pass attachment_id="${sendableIds[0]}" to outlook_send_mail, teams_send_chat_message, or teams_send_message. Do NOT rebuild the file — use the attachment_id directly.]`
            : `\n\n[These ${sendableIds.length} uploaded files are stored on the server. To send/share them via Outlook or Teams, pass attachment_ids=${JSON.stringify(sendableIds)} to outlook_send_mail, teams_send_chat_message, or teams_send_message. Do NOT rebuild the files — use the attachment_ids array directly.]`
          : "";
        toSend = `${blocks}${sendHint}\n\nUser question: ${toSend}`;
      }

      // For non-extracted files (pdf, txt, etc.) tell the agent to read them by path.
      // IMPORTANT: use extract_document (not Read) for binary formats — Read is Claude's
      // native tool and cannot parse binary .docx/.xlsx/.pdf etc.
      if (byPath.length) {
        const refs = byPath
          .map((f) => {
            const idPart = f.serverId ? ` [attachment_id=${f.serverId}]` : "";
            return f.path ? `${f.name} (${f.path})${idPart}` : `${f.name}${idPart}`;
          })
          .join(", ");
        const byPathServerIds = byPath.filter((f) => f.serverId).map((f) => f.serverId);
        const byPathSendHint = byPathServerIds.length
          ? byPathServerIds.length === 1
            ? ` To send/share/email this file, use attachment_id="${byPathServerIds[0]}" in outlook_send_mail, teams_send_chat_message, or teams_send_message — do NOT rebuild the file.`
            : ` To send/share/email these files, use attachment_ids=${JSON.stringify(byPathServerIds)} in outlook_send_mail, teams_send_chat_message, or teams_send_message — do NOT rebuild the files.`
          : ` If the user asks to send/share/post/email these files, inform them that the file could not be uploaded to the server (the upload may have failed due to a network issue). Ask them to try attaching the file again via the 📎 button — once uploaded successfully, you will have an attachment_id to use. Do NOT use attachment_file_path — the server cannot access local Windows files.`;
        toSend = `[The user attached file(s): ${refs}. To read/analyse them call \`extract_document\` with the file path — do NOT use the Read tool for binary files (docx, xlsx, pdf, pptx, etc.) as it cannot parse them. For plain-text files (csv, txt, md, json) you may use Read.${byPathSendHint}]\n\n${toSend}`;
      }

      // Only clear the composer's attachment chips when sending the user's current
      // message (not when auto-dequeuing a previously queued message whose
      // attachments were already captured at enqueue time).
      if (!isAutoDequeue) setAttachedFiles([]);
    }
    // Remember whether this turn is a DOCUMENT request, so a CLI-missing exit
    // (session:exit code -1) can re-route it through the REST fallback below.
    if (!isSlash) {
      const det = detectDocRequest(text);
      if (det) pendingDocReqRef.current[id] = { text, det };
      else delete pendingDocReqRef.current[id];
    }
    // ── Auto-upload folder files when user wants to send/share them ─────────
    // Reuses the EXACT paperclip upload path (readFileBinary IPC → Blob →
    // uploadFileToServer), which already works for every document type. The
    // uploaded file gets a server-side ChatAttachment id; we inject those ids as
    // attachment_ids so the AI passes them straight to outlook_send_mail /
    // teams_send_chat_message — identical to how a paperclip-attached file flows.
    // Only triggers when: folder is set, files exist, and the message has both a
    // send intent and a file intent.
    // Intent words are deliberately broad: users write "document", "report", "deck",
    // and typo the verb ("attaach"). A miss here silently skips the whole upload, so
    // the cost of being too narrow is much higher than being slightly too eager —
    // the file-name/type matching below still decides WHICH files are uploaded.
    const SEND_INTENT = /\b(se+nd|sha?re|e?mail|at+a+ch+(?:ed|ment|ments)?|fo?rwa?rd|post|share|deliver)\w*\b/i;
    const FILE_INTENT = new RegExp(
      "\\b(" +
      "files?|docs?|documents?|attachments?|" +
      "docx|pdfs?|pptx?|xlsx?|xls|xlsm|txt|csv|odt|ods|rtf|md|" +
      "reports?|decks?|presentations?|slides?|spreadsheets?|workbooks?|sheets?|" +
      "words?|excels?|powerpoints?|images?|photos?|pictures?|screenshots?|" +
      "everything|all" +
      ")\\b", "i");
    const hasBinaryIpcSend = typeof readFileBinary === "function";
    if (!isSlash && folder && files.length > 0 && hasBinaryIpcSend &&
        SEND_INTENT.test(text) && FILE_INTENT.test(text)) {
      const textLower = text.toLowerCase();
      const SEND_EXTS = ["docx","pdf","pptx","xlsx","txt","doc","xls","xlsm","csv","odt","ods","ppt","rtf","md","json","xml","html","htm","png","jpg","jpeg","gif","webp","bmp"];
      // Treat _ - . and whitespace as EQUIVALENT separators on BOTH sides, so
      // "cricket turf" matches "cricket_turf_specifications.docx". Previously the
      // underscore was preserved, so a space-separated reference never matched.
      const normalize = (s) => s.toLowerCase()
        .replace(/[^a-z0-9]+/g, " ")   // any run of non-alphanumerics -> single space
        .replace(/\s+/g, " ").trim();
      const textNorm = normalize(text);

      // Helper: does the message EXACTLY name this folder file?
      // Only rules 1 & 2 — strict verbatim / separator-normalised containment.
      // Rule 3 (partial word matching) is intentionally removed: when the user
      // types a long filename like "adarsh_singh_report.pdf", every word in that
      // stem also appears in the message, causing every other file that shares
      // even two of those tokens (e.g. all other adarsh_singh_* files) to be
      // swept in. Exact matching is sufficient — if the user typed the filename
      // it will always hit rule 1 or 2.
      const isNamed = (relPath) => {
        const base = (relPath.split(/[\/\\]/).pop() || "");
        const baseL = base.toLowerCase();
        const stem = baseL.replace(/\.[^.]+$/, "");
        const baseNorm = normalize(base);
        const stemNorm = normalize(stem);
        // 1. Exact filename typed verbatim (e.g. "report.pdf" in message).
        if (textLower.includes(baseL)) return true;
        // 2. Separator-insensitive full-name / stem containment
        //    (e.g. "cricket turf specifications" matches "cricket_turf_specifications.docx").
        if (baseNorm && textNorm.includes(baseNorm)) return true;
        if (stemNorm.length >= 4 && textNorm.includes(stemNorm)) return true;
        return false;
      };

      // PASS 1 — explicitly named files win: "send report.pdf" or
      // "send cricket_turf_specifications.docx" → upload EXACTLY those, nothing else.
      let toUpload = files.filter((f) => {
        const ext = (f.split(".").pop() || "").toLowerCase();
        return SEND_EXTS.includes(ext) && isNamed(f);
      });

      // PASS 2 — no explicit filename → fall back to type/extension intent
      // ("all docs and pdf", "the excels", or generic "these files"/"all").
      // Word/keyword aliases matter: "docs"/"word" must include .docx (not just
      // literal "doc"), otherwise "all docs and pdf" matched only the PDF.
      if (toUpload.length === 0) {
        const TYPE_ALIASES = [
          [/\b(docx?|docs?|word)\b/,             ["docx", "doc", "odt", "rtf"]],
          [/\b(pdf|pdfs)\b/,                     ["pdf"]],
          [/\b(pptx?|ppts?|presentations?|slides?|powerpoint)\b/, ["pptx", "ppt"]],
          [/\b(xlsx?|xls|excels?|spreadsheets?|workbooks?|sheets?)\b/, ["xlsx", "xls", "xlsm", "ods", "csv"]],
          // "txt"/"text" means the .txt extension ONLY — .md (Markdown notes) is a
          // different file type and must never be swept in by this word. Match
          // "notes/markdown" separately if that wording is ever needed.
          [/\b(txt|text)\b/,                     ["txt"]],
          [/\b(notes?|markdown)\b/,               ["md"]],
          [/\b(csv)\b/,                          ["csv"]],
          [/\b(images?|photos?|pictures?|screenshots?)\b/, ["png", "jpg", "jpeg", "gif", "webp", "bmp"]],
        ];
        const wanted = new Set();
        for (const [re, exts] of TYPE_ALIASES) {
          if (re.test(textLower)) exts.forEach((e) => wanted.add(e));
        }
        // Also honour any bare extension the user typed literally (e.g. "xlsm").
        SEND_EXTS.forEach((e) => { if (new RegExp(`\\b${e}\\b`).test(textLower)) wanted.add(e); });
        const matchesType = (f) => {
          const ext = (f.split(".").pop() || "").toLowerCase();
          return wanted.size > 0 ? wanted.has(ext) : SEND_EXTS.includes(ext);
        };
        // "current dir/folder" (and, by default, everyday phrasing like "the
        // files") means files directly in the attached folder — NOT everything in
        // every subfolder. Prefer top-level matches; only fall back to the full
        // recursive list if nothing matches at the top level, so a request for
        // "all X" from a folder with nested project subfolders doesn't silently
        // pull in unrelated nested files.
        const isTopLevel = (f) => !/[\\/]/.test(f);
        const topLevelMatches = files.filter((f) => isTopLevel(f) && matchesType(f));
        toUpload = topLevelMatches.length > 0 ? topLevelMatches : files.filter(matchesType);
      }
      // ── Per-message cap: silently trim to MAX_SEND, tell the AI what was cut ──
      // No popup — just cap and let the AI inform the user in its reply.
      const MAX_SEND = 10;
      let overCapNote = "";
      if (toUpload.length > MAX_SEND) {
        const origCount = toUpload.length;
        toUpload = toUpload.slice(0, MAX_SEND);
        overCapNote = `[NOTE: the user requested ${origCount} files, but only the first ${MAX_SEND} are attached (per-message limit). `
                    + `Clearly tell the user that ${origCount - MAX_SEND} file(s) were NOT attached because of the ${MAX_SEND}-file limit, `
                    + `and ask them to narrow their request (e.g. name specific files or a file type).]`;
      }

      if (toUpload.length > 0) {
        const MIME_MAP = {
          docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          doc:  "application/msword",
          pdf:  "application/pdf",
          pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
          ppt:  "application/vnd.ms-powerpoint",
          xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          xls:  "application/vnd.ms-excel",
          xlsm: "application/vnd.ms-excel.sheet.macroEnabled.12",
          csv:  "text/csv",
          txt:  "text/plain",
          odt:  "application/vnd.oasis.opendocument.text",
          ods:  "application/vnd.oasis.opendocument.spreadsheet",
          rtf:  "application/rtf",
          md:   "text/markdown",
          json: "application/json",
          xml:  "application/xml",
          html: "text/html", htm: "text/html",
          png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
          gif: "image/gif", webp: "image/webp", bmp: "image/bmp",
        };
        // Convert base64 → Blob (same helper the paperclip TIER 3 path uses).
        const base64ToBlob = (b64, mime) => {
          const binary = atob(b64);
          const bytes  = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
          return new Blob([bytes], { type: mime });
        };
        const results = await Promise.all(toUpload.map(async (relPath) => {
          const sep = folder.includes("\\") ? "\\" : "/";
          const absPath = `${folder}${sep}${relPath}`;
          const name = relPath.split(/[\/\\]/).pop();
          const ext  = (name.split(".").pop() || "").toLowerCase();
          try {
            // Read the file bytes in the renderer, then upload through the SAME
            // uploadFileToServer useCallback the paperclip uses — proven to work
            // for all binary document formats.
            const { base64, error: readErr } = await readFileBinary(absPath);
            if (readErr || !base64) {
              return { name, serverId: null, reason: readErr || "could not read file" };
            }
            const mime = MIME_MAP[ext] || "application/octet-stream";
            const blob = base64ToBlob(base64, mime);
            // Bounded retry for TRANSIENT failures only (network drop / HTTP 429
            // rate-limit / HTTP 5xx) — the gateway caps uploads at 30 / 5 min, so a
            // big batch can hit a transient 429 that a short backoff clears. We call
            // the SHARED uploadFileToServer here (unchanged) and just retry around
            // it; hard failures (blocked-by-policy, rejected) are NOT retried.
            const isTransient = (e) => {
              const m = String(e?.message || e || "").toLowerCase();
              return m.includes("429") || m.includes("rate limit") || m.includes("timeout")
                  || m.includes("network") || m.includes("failed to fetch")
                  || / http 5\d\d/.test(m) || /http 5\d\d/.test(m);
            };
            let up, lastErr;
            for (let attempt = 0; attempt < 3; attempt++) {
              try {
                up = await uploadFileToServer(blob, name);
                lastErr = null;
                break;
              } catch (e) {
                lastErr = e;
                if (attempt < 2 && isTransient(e)) {
                  await new Promise((r) => setTimeout(r, 600 * (attempt + 1))); // 0.6s, 1.2s
                  continue;
                }
                break; // non-transient, or out of attempts
              }
            }
            if (lastErr) throw lastErr;
            if (up?.serverId) return { name, serverId: up.serverId };
            return {
              name, serverId: null,
              reason: up?.blocked
                ? `blocked by content policy${up.blockReason ? ` (${up.blockReason})` : ""}`
                : "upload rejected by server",
            };
          } catch (err) {
            const reason = err?.message || String(err);
            console.error("[auto-upload] failed for", name, "-", reason);
            return { name, serverId: null, reason };
          }
        }));
        const autoUploaded = results.filter((r) => r && r.serverId);
        const autoFailed   = results.filter((r) => r && !r.serverId);

        // ── Partial-failure: proceed silently with whatever uploaded OK ──────────
        // No popup — the AI is told which files failed and will report it to the user.

        if (autoUploaded.length > 0) {
          const ids = autoUploaded.map((u) => u.serverId);
          const autoHint = ids.length === 1
            ? `[Folder file auto-uploaded to server: "${autoUploaded[0].name}" = attachment_id "${ids[0]}". ` +
              `If you tell the user this file will be sent, pass attachment_ids=["${ids[0]}"] to ` +
              `outlook_send_mail / teams_send_chat_message.]`
            : `[${ids.length} folder file(s) auto-uploaded to server, mapped name -> attachment_id:\n` +
              autoUploaded.map((u) => `  "${u.name}" = "${u.serverId}"`).join("\n") +
              `\nCRITICAL — INTEGRITY RULE: this is the set of files AVAILABLE to attach, NOT an instruction ` +
              `to attach all of them. Attach ONLY the attachment_ids for the exact files you list/confirm ` +
              `with the user (e.g. by name in your message or in a confirmation prompt). If you tell the ` +
              `user "N files" or name specific files, the attachment_ids you pass to the send tool MUST be ` +
              `exactly those N files' ids from the map above — never more, never fewer, never the full list ` +
              `by default. Do not invent an id and do not drop one you did confirm.]`;
          toSend = `${autoHint}\n\n${toSend}`;
        }
        // Tell the agent which files failed so it reports that in its reply (don't rebuild/retry).
        if (autoFailed.length > 0) {
          const failList = autoFailed.map((f) => `${f.name} — ${f.reason}`).join("; ");
          toSend = `[NOTE: these folder file(s) could NOT be uploaded and therefore CANNOT be attached: ${failList}. Do NOT retry uploading them, do NOT use upload_file_to_chat, and do NOT rebuild them with build_document. Send the successfully attached files and clearly tell the user which file(s) could not be attached and why.]\n\n${toSend}`;
        }
        // If we trimmed to the per-message cap, make the AI state that too.
        if (overCapNote) {
          toSend = `${overCapNote}\n\n${toSend}`;
        }
      }
    }
    // ── End auto-upload ───────────────────────────────────────────────────────

    // Pass the durable conversation id so the desktop injects x-ainxt-conv-id
    // into config.toml before each turn — the gateway's Redis-backed Buddy
    // history pipeline keys on this header to save/restore context across
    // resume failures and app restarts. Without it conv_id="" and the pipeline
    // is silently skipped, leaving history only as durable as the Postgres save.
    coworkOfficeRun(id, toSend, model, convIdRef.current || null);
  }, [chatInput, folder, attachedFiles, ensureChatSession, model, state.convs, modelLocked, lockedModel, files, uploadFileToServer, confirm, uiToast, chatBusy, enqueue, isFull, maxWait, setQueuedCount]);

  // Ref bridge so the mount-once coworkOfficeOnEvent subscription always calls
  // the LATEST sendChat closure. Without this, auto-dequeue fires the stale
  // mount-time closure whose state.convs snapshot is empty — causing USER_TURN
  // to overwrite the real conversation with an empty message list (Bug 1) and
  // the prior-exchange transcript to be built from stale/empty data, giving the
  // LLM the wrong context for the queued message (Bug 2).
  const sendChatRef = useRef(null);
  useEffect(() => { sendChatRef.current = sendChat; }, [sendChat]);

  const regenerate = useCallback(async () => {
    const cid = chatIdRef.current;
    const conv = cid ? convsRef.current[cid] : null;
    if (!conv || conv.status === "running") return;
    const lastUser = [...conv.messages].reverse().find((m) => m.role === "user");
    const text = lastUser?.blocks?.[0]?.text;
    if (!text) return;
    dispatch({ type: "USER_TURN", id: cid, text });
    coworkOfficeRun(cid, text, model, convIdRef.current || null);
  }, [model]);

  // Answer a permission prompt: send the response to the CLI. This is the ONLY way
  // a connector write/send proceeds — there is no auto-execute path.
  const onPermissionAnswer = useCallback((sessionId, confirmId, answer) => {
    const conv = state.convs[sessionId];
    if (!conv?.pendingConfirm) return;
    coworkOfficeRespondConfirm(sessionId, confirmId, answer);
    dispatch({ type: "EVENT", id: sessionId, event: { type: "__clear_confirm" } });
  }, [state.convs]);

  // ── Render gates ──────────────────────────────────────────────────────────
  if (!isCoworkOfficeAvailable) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <div className="max-w-md text-center">
          <MonitorSmartphone className="w-10 h-10 text-indigo-500 mx-auto mb-3" />
          <h2 className="text-lg font-semibold text-gray-800 mb-1">Local Buddy runs in the desktop app</h2>
          <p className="text-sm text-gray-500">The local office agent works on your machine, connectors, and documents — which a browser can't access. Open AiNxt Desktop to use it (the browser uses the server-side office assistant instead).</p>
        </div>
      </div>
    );
  }

  // While the auth check is still in flight (initial mount or tab-return), show a
  // neutral spinner instead of the login screen. This prevents the "Sign in" button
  // from flashing for 1-4 seconds while validateToken does its HTTP round-trip —
  // which was the "already signed in but sees login screen until I click another tab" bug.
  if (auth.error === "loading") {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <div className="flex flex-col items-center gap-3 text-gray-400">
          <svg className="animate-spin w-7 h-7 text-indigo-500" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z" />
          </svg>
          <span className="text-sm">Checking session…</span>
        </div>
      </div>
    );
  }

  if (!auth.authenticated) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <div className="max-w-lg w-full">
          <div className="text-center mb-4">
            <Briefcase className="w-10 h-10 text-indigo-500 mx-auto mb-3" />
            <h2 className="text-lg font-semibold text-gray-800 mb-1">
              {auth.error === "expired" ? "Session expired" : "Enable local office mode"}
            </h2>
            <p className="text-sm text-gray-500">
              {auth.error === "expired"
                ? "Your AiNxt session is no longer valid. Sign in again to keep using Buddy."
                : "Sign in to the AiNxt CLI once to let the office agent work with your documents and connected apps."}
            </p>
          </div>
          <button onClick={handleLogin} disabled={loggingIn}
            className="w-full bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-lg py-2.5 text-sm font-medium">
            {loggingIn ? "Signing in…" : "Sign in to AiNxt"}
          </button>
          {adoptError && (
            <div className="mt-3 text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded-lg p-3">
              {adoptError}
            </div>
          )}
          {loginLog && (
            <div className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm">
              {loggingIn && (
                <div className="flex items-center gap-2 text-gray-600 mb-2">
                  <Loader2 size={14} className="animate-spin shrink-0" />
                  <span>Signing in — please wait…</span>
                </div>
              )}
              {loginUrl && (
                <button
                  onClick={() => openExternal(loginUrl)}
                  className="w-full mb-2 flex items-center justify-center gap-2 bg-indigo-50 hover:bg-indigo-100 border border-indigo-200 text-indigo-700 rounded-lg py-2 text-sm font-medium transition-colors">
                  Open browser to complete sign-in →
                </button>
              )}
              <button
                onClick={() => setShowLoginDetails((v) => !v)}
                className="text-xs text-gray-400 hover:text-gray-600 underline-offset-2 hover:underline">
                {showLoginDetails ? "Hide technical details ▴" : "Show technical details ▾"}
              </button>
              {showLoginDetails && (
                <pre className="mt-2 text-xs text-gray-500 max-h-40 overflow-auto whitespace-pre-wrap">{loginLog}</pre>
              )}
            </div>
          )}
          {/* Manual API key entry — alternative path for first-time users */}
          <div className="mt-4 border-t border-gray-100 pt-4">
            <p className="text-xs text-gray-400 mb-2 text-center">Or connect with an API key</p>
            <div className="flex gap-2">
              <input
                type="password"
                placeholder="Paste your API key…"
                value={manualKey}
                onChange={(e) => { setManualKey(e.target.value); setManualKeyError(""); }}
                onKeyDown={(e) => { if (e.key === "Enter" && manualKey.trim()) handleManualKey(); }}
                className="flex-1 text-sm border border-gray-200 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-300"
              />
              <button
                onClick={handleManualKey}
                disabled={!manualKey.trim() || loggingIn}
                className="text-sm bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-white rounded-lg px-3 py-2 font-medium transition-colors">
                Connect
              </button>
            </div>
            {manualKeyError && (
              <p className="mt-2 text-xs text-red-600">{manualKeyError}</p>
            )}
          </div>
        </div>
      </div>
    );
  }

  // True while openConversation's fast-path has shown messages but the real
  // session is still being created (token validation + CLI handshake). The send
  // button is disabled during this window so the user can't send before the
  // session is ready.
  const chatSessionPending = !!(chatId && chatId.startsWith("__placeholder_"));

  const onModelChange = (value) => {
    if (modelLocked) return;  // switching disabled while pinned to lockedModel
    setModel(value); if (chatIdRef.current) coworkOfficeSetModel(chatIdRef.current, value);
  };
  const onPermModeChange = (value) => { setPermMode(value); if (chatIdRef.current) coworkOfficeSetPermissionMode(chatIdRef.current, value); };
  // Switching role/plugin re-specializes the agent → start a fresh session (the
  // role's prompt + scoped tools are applied at spawn). roleRef updates via effect.
  const onRoleChange = (value) => { setRoleId(value); newChat(); };

  // Unified completion menu: "/" slash-commands, or "@" file-mentions (only when a
  // working folder is open).
  // Buddy is an OFFICE assistant, not Code — only curated OFFICE slash commands
  // (no developer commands, no /model — model lives in the footer dropdown).
  const allCmds = [{ name: "schedule", description: "Run this as a recurring task (daily/weekly/monthly)", argumentHint: "<what to do>" }];
  const slashTyping = chatInput.startsWith("/") && !chatInput.includes("\n");
  const atMatch = folder ? chatInput.match(/(^|\s)@([^\s@]*)$/) : null;
  let compItems = [];
  if (slashTyping) {
    const q = chatInput.slice(1).toLowerCase();
    compItems = allCmds.filter((c) => c.name.toLowerCase().startsWith(q)).slice(0, 50).map((c) => ({
      label: `/${c.name}`, hint: c.argumentHint, desc: c.description, insert: `/${c.name} `,
    }));
  } else if (atMatch) {
    const q = atMatch[2].toLowerCase();
    const base = chatInput.slice(0, atMatch.index + atMatch[1].length);
    compItems = files
      .filter((f) => f.toLowerCase().includes(q))
      .sort((a, b) => a.length - b.length)
      .slice(0, 50)
      .map((f) => ({ label: `@${f}`, hint: "", desc: "", insert: `${base}@${f} ` }));
  }
  const compOpen = compItems.length > 0 && !compDismissed;
  const applyCompletion = (item) => {
    if (!item) return;
    setChatInput(item.insert);
    setCompDismissed(false);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };
  const onInputKeyDown = (e) => {
    if (compOpen) {
      if (e.key === "ArrowDown") { e.preventDefault(); setCompIdx((i) => (i + 1) % compItems.length); return; }
      if (e.key === "ArrowUp")   { e.preventDefault(); setCompIdx((i) => (i - 1 + compItems.length) % compItems.length); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); applyCompletion(compItems[Math.min(compIdx, compItems.length - 1)]); return; }
      if (e.key === "Escape")    { e.preventDefault(); setCompDismissed(true); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!chatSessionPending) sendChat(); }
  };

  const ctxPct = typeof chat?.contextPct === "number" ? Math.round(chat.contextPct) : null;
  const sessionCost = chat?.costTotal;

  // Sidebar list = the live conversation (shown immediately) + the persisted ones,
  // FILTERED to the selected project (so each project shows only its own tasks).
  const projById = Object.fromEntries(projects.map((p) => [p.id, p]));
  const _bucketName = folder ? ((folder.split("/").filter(Boolean).pop()) || "Project") : "Office";
  const _activeRawTitle = chat?.messages?.find((m) => m.role === "user")?.blocks?.[0]?.text || "";
  const _activeTitle = _activeRawTitle ? cleanConvTitle(_activeRawTitle) : "";
  const displayConvs = conversations.filter((c) => (c.projectId || "") === (activeProjectId || ""));
  if (convId && _activeTitle && !displayConvs.some((c) => c.id === convId)) {
    displayConvs.unshift({ id: convId, folder, folderName: _bucketName, projectId: activeProjectId || "", title: _activeTitle });
  }

  // Real upload percentage while attaching, averaged across files currently
  // uploading to the server. null while every file is still in a pre-upload
  // local-parsing tier (spreadsheet IPC / local read) with no byte progress
  // to report yet — the indeterminate bar is shown in that case instead.
  const _attachPcts = Object.values(attachProgress);
  const attachPct = _attachPcts.length && _attachPcts.some((v) => v > 0)
    ? Math.round(_attachPcts.reduce((a, b) => a + b, 0) / _attachPcts.length)
    : null;

  return (
    <div className="h-full flex flex-col bg-white relative">
      {/* Transient success banner (e.g. after scheduling a recurring task) */}
      {toast && (
        <div className="absolute top-3 left-1/2 -translate-x-1/2 z-40 max-w-xl">
          <div className="flex items-start gap-2 bg-green-50 border border-green-300 text-green-800 text-sm rounded-lg px-4 py-2.5 shadow-md">
            <span className="flex-1">{toast}</span>
            <button onClick={() => setToast("")} className="text-green-500 hover:text-green-700 shrink-0">✕</button>
          </div>
        </div>
      )}
      {/* Scheduler — dedicated Buddy scheduled-tasks panel (list + detail + 7-day history) */}
      {(showSchedule || showSchedules) && (
        <CoworkScheduler
          projectId={activeProjectId || ""}
          projectName={activeProject?.name || ""}
          roles={roles}
          initialCreate={showSchedule}
          initialPrompt={schedulePrompt}
          onToast={(m) => { setToast(m); setTimeout(() => setToast(""), 8000); }}
          onClose={() => { setShowSchedule(false); setShowSchedules(false); setSchedulePrompt(""); }}
        />
      )}

      {/* Project editor — name + instructions + PERSISTENT memory */}
      {editingProject && (
        <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/30" onMouseDown={() => setEditingProject(null)}>
          <div className="bg-white rounded-xl shadow-xl border border-gray-200 w-[36rem] p-5" onMouseDown={(e) => e.stopPropagation()}>
            <h3 className="font-semibold text-gray-800 mb-1">{editingProject.id ? "Edit project" : "New project"}</h3>
            <p className="text-xs text-gray-500 mb-3">A project groups related tasks and gives Buddy standing <b>instructions</b>, a <b>persistent memory</b>, and an optional <b>document folder</b> — all of which apply to every task in the project, so you never repeat yourself. Tasks you start in a project are listed only under it.</p>
            <label className="block text-xs font-medium text-gray-600 mb-1">Name</label>
            <input value={editingProject.name} onChange={(e) => setEditingProject({ ...editingProject, name: e.target.value })}
              placeholder="e.g. Q3 Settlement Review"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400 mb-3" />
            <label className="block text-xs font-medium text-gray-600 mb-1">Instructions <span className="text-gray-400 font-normal">(how Buddy should work in this project)</span></label>
            <textarea rows={3} value={editingProject.instructions || ""} onChange={(e) => setEditingProject({ ...editingProject, instructions: e.target.value })}
              placeholder="e.g. Always summarise for the CFO; use formal tone; figures in INR crore."
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400 mb-3" />
            <label className="block text-xs font-medium text-gray-600 mb-1">Project memory <span className="text-gray-400 font-normal">(facts/context to remember across tasks)</span></label>
            <textarea rows={4} value={editingProject.memory || ""} onChange={(e) => setEditingProject({ ...editingProject, memory: e.target.value })}
              placeholder="e.g. Settlement team channel = #ops-settlement. Key contacts: …. Recurring deck template = AiNxt corporate."
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400 mb-3" />
            <label className="block text-xs font-medium text-gray-600 mb-1">Document folder <span className="text-gray-400 font-normal">(the agent can only read files inside this folder for this project)</span></label>
            <div className="flex items-center gap-2 mb-3">
              <button onClick={async () => { const f = await pickFolder(); if (f) setEditingProject({ ...editingProject, folder: f }); }}
                className="px-3 py-1.5 text-sm rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 shrink-0">Choose folder…</button>
              <span className="text-xs text-gray-500 truncate flex-1" title={editingProject.folder || ""}>
                {editingProject.folder || "No folder — works from connectors + documents only"}
              </span>
              {editingProject.folder && (
                <button onClick={() => setEditingProject({ ...editingProject, folder: null })}
                  className="text-xs text-gray-400 hover:text-red-600 shrink-0">clear</button>
              )}
            </div>
            <div className="flex justify-end gap-2">
              {editingProject.id && (
                <button onClick={() => { removeProject(editingProject.id); setEditingProject(null); }}
                  className="mr-auto px-3 py-1.5 text-sm rounded-md border border-red-200 text-red-600 hover:bg-red-50">Delete</button>
              )}
              <button onClick={() => setEditingProject(null)} className="px-3 py-1.5 text-sm rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50">Cancel</button>
              <button onClick={saveProject} disabled={!(editingProject.name || "").trim()}
                className="px-3 py-1.5 text-sm rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40">Save project</button>
            </div>
          </div>
        </div>
      )}

      {/* Memory — what Buddy durably remembers about you (prefs + saved facts) */}
      {showMemory && (
        <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/30" onMouseDown={() => setShowMemory(false)}>
          <div className="bg-white rounded-xl shadow-xl border border-gray-200 w-[40rem] max-h-[85vh] flex flex-col p-5" onMouseDown={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 mb-1">
              <Brain className="w-4 h-4 text-indigo-600" />
              <h3 className="font-semibold text-gray-800">What Buddy remembers about you</h3>
              <button onClick={() => setShowMemory(false)} className="ml-auto text-gray-400 hover:text-gray-700"><X className="w-4 h-4" /></button>
            </div>
            <p className="text-xs text-gray-500 mb-3">Stored on the server and applied to every task — so you never repeat yourself. These shape style and defaults only; sending or writing anything still needs your confirmation. Never put passwords or card numbers here.</p>
            {memPrefs === null ? (
              <div className="flex items-center gap-2 text-sm text-gray-400 py-6"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
            ) : (
              <div className="flex-1 overflow-y-auto space-y-4">
                {/* Preferences. role = free text (any job title; used as natural-
                    language context, so it needs hints, not validation). tone +
                    format = fixed sets the system actually acts on → dropdowns. */}
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Your role</label>
                    <input list="buddy-role-suggestions" defaultValue={memPrefs.role || ""}
                      onBlur={(e) => saveMemPref("role", e.target.value)}
                      placeholder="Start typing… e.g. Engineering Manager"
                      className="w-full border border-gray-300 rounded-lg px-3 py-1.5 text-sm outline-none focus:border-gray-400" />
                    <datalist id="buddy-role-suggestions">
                      <option value="Engineering Manager" /><option value="Software Engineer" />
                      <option value="Product Manager" /><option value="Business Analyst" />
                      <option value="Project Manager" /><option value="Operations Manager" />
                      <option value="Finance Analyst" /><option value="HR Manager" />
                      <option value="Executive / Leadership" /><option value="Compliance Officer" />
                    </datalist>
                    <p className="text-[11px] text-gray-400 mt-0.5">Type any title — Buddy uses it to pitch detail &amp; tone for you.</p>
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Preferred tone</label>
                    <select defaultValue={memPrefs.tone || ""} onChange={(e) => saveMemPref("tone", e.target.value)}
                      className="w-full border border-gray-300 rounded-lg px-2 py-1.5 text-sm bg-white outline-none">
                      <option value="">No preference</option>
                      <option value="formal">Formal</option>
                      <option value="concise">Concise</option>
                      <option value="friendly">Friendly</option>
                      <option value="detailed">Detailed</option>
                      <option value="neutral">Neutral / professional</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Default document format</label>
                    <select defaultValue={memPrefs.default_doc_format || ""} onChange={(e) => saveMemPref("default_doc_format", e.target.value)}
                      className="w-full border border-gray-300 rounded-lg px-2 py-1.5 text-sm bg-white outline-none">
                      <option value="">No preference</option>
                      <option value="docx">Word (.docx)</option>
                      <option value="pdf">PDF</option>
                      <option value="pptx">PowerPoint (.pptx)</option>
                      <option value="xlsx">Excel (.xlsx)</option>
                      <option value="md">Markdown</option>
                    </select>
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Email signature <span className="text-gray-400 font-normal">(used when drafting emails)</span></label>
                  <textarea defaultValue={memPrefs.email_signature || ""} onBlur={(e) => saveMemPref("email_signature", e.target.value)} rows={2}
                    placeholder={"Regards,\nYour Name — AiNxt"}
                    className="w-full border border-gray-300 rounded-lg px-3 py-1.5 text-sm outline-none focus:border-gray-400" />
                </div>

                {/* Remembered facts */}
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Remembered facts <span className="text-gray-400 font-normal">(Buddy saves these as it learns — you can forget any)</span></label>
                  <div className="space-y-1.5 mb-2">
                    {(Array.isArray(memPrefs.memory_notes) ? memPrefs.memory_notes : []).length === 0 && (
                      <p className="text-xs text-gray-400">Nothing yet. Buddy remembers lasting facts as you work — or add one below.</p>
                    )}
                    {(Array.isArray(memPrefs.memory_notes) ? memPrefs.memory_notes : []).slice().reverse().map((n, i) => (
                      <div key={`${n}-${i}`} className="flex items-start gap-2 bg-gray-50 border border-gray-200 rounded-lg px-3 py-1.5">
                        <span className="text-sm text-gray-700 flex-1">{n}</span>
                        <button onClick={() => delMemNote(n)} title="Forget this"
                          className="shrink-0 p-0.5 text-gray-400 hover:text-red-600"><X className="w-3.5 h-3.5" /></button>
                      </div>
                    ))}
                  </div>
                  <div className="flex items-center gap-2">
                    <input value={memNote} onChange={(e) => setMemNote(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addMemNote(); } }}
                      placeholder="Add a fact for Buddy to remember…"
                      className="flex-1 border border-gray-300 rounded-lg px-3 py-1.5 text-sm outline-none focus:border-gray-400" />
                    <button onClick={addMemNote} disabled={memBusy || !memNote.trim()}
                      className="px-3 py-1.5 text-sm rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40 flex items-center gap-1.5 shrink-0">
                      {memBusy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Add
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Top bar */}
      <div className="flex items-center gap-3 px-6 py-3 bg-white border-b border-gray-200 shrink-0">
        <Briefcase className="w-6 h-6 text-indigo-600" />
        <div className="flex flex-col">
          <h1 className="text-base font-semibold text-gray-900 leading-tight">Buddy</h1>
          <p className="text-xs text-gray-500 leading-tight">Local office agent — connectors, documents, and your apps</p>
        </div>
        {/* What Buddy remembers about you (durable, server-side, you control it). */}
        <button onClick={openMemory}
          className="ml-auto flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-700"
          title="What Buddy remembers about you — view, add, or forget facts and preferences">
          <Brain className="w-4 h-4 text-indigo-600" /> Memory
        </button>
        {/* Folder is OPTIONAL. When set, the agent may read relevant files from it
            to ground its work. Clear it for pure generation (no folder rummaging). */}
        <div className="flex items-center">
          <button onClick={chooseFolder}
            className="flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-700"
            title="Optional: a local folder the agent can read from / save into. Clear it for pure generation.">
            <FolderOpen className="w-4 h-4 text-indigo-600" />
            {folder ? <span className="font-mono text-xs truncate max-w-[18rem]">{folder}</span> : "Working folder (optional)"}
          </button>
          {folder && (
            <button onClick={() => { folderRef.current = null; setFolder(null); setAttachedFiles([]); }}
              title="Clear working folder (stop reading local files — pure generation)"
              className="ml-1 p-1 rounded-md text-gray-400 hover:text-red-600 hover:bg-gray-50 cursor-pointer">
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* Body: history rail + chat column */}
      <div className="flex-1 flex min-h-0">
        {/* History rail — past conversations (resume on click) */}
        <div className="w-60 border-r border-gray-200 bg-gray-50 flex flex-col shrink-0">
          {/* Project selector — its instructions + persistent memory apply to tasks */}
          <div className="p-2 pb-1">
            <div className="flex items-center gap-1 mb-1">
              <span className="text-[11px] uppercase tracking-wide text-gray-400">Project</span>
              <button onClick={() => setEditingProject({ name: "", instructions: "", memory: "" })}
                title="New project" className="ml-auto text-gray-400 hover:text-indigo-600"><Plus className="w-3.5 h-3.5" /></button>
              {activeProject && (
                <button onClick={() => setEditingProject({ ...activeProject })}
                  title="Edit project (instructions + memory)" className="text-gray-400 hover:text-indigo-600 text-[11px]">edit</button>
              )}
            </div>
            <select value={activeProjectId} onChange={(e) => {
                const id = e.target.value;
                const p = projects.find((x) => x.id === id) || null;
                // Set refs synchronously so the new session/task gets THIS project +
                // its folder (the project's document scope), before newChat runs.
                projectRef.current = p ? { name: p.name, instructions: p.instructions || "", memory: p.memory || "", folder: p.folder || null } : null;
                activeProjectIdRef.current = id;
                setActiveProjectId(id);
                if (p && p.folder) { folderRef.current = p.folder; setFolder(p.folder); }
                newChat();
              }}
              className="w-full text-sm border border-gray-200 rounded-lg px-2 py-1.5 bg-white outline-none cursor-pointer">
              <option value="">No project (general)</option>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </div>
          <div className="p-2 pt-1 space-y-1.5">
            <button onClick={newChat}
              className="w-full flex items-center justify-center gap-1.5 text-sm bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg py-1.5">
              <Plus className="w-4 h-4" /> New task
            </button>
            <button onClick={() => setShowSchedules(true)}
              className="w-full flex items-center justify-center gap-1.5 text-xs border border-gray-200 text-gray-600 hover:bg-white rounded-lg py-1.5">
              <Clock className="w-3.5 h-3.5" /> {activeProject ? "Scheduled in this project" : "Scheduled tasks"}
            </button>
          </div>
          <div className="px-3 pb-1 text-[11px] uppercase tracking-wide text-gray-400">Tasks</div>
          <div className="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5">
            {displayConvs.length === 0 && (
              <p className="text-xs text-gray-400 px-2 mt-2">Send a message — your tasks appear here.</p>
            )}
            {displayConvs.map((c) => (
              <div key={c.id} className={`group relative rounded-md ${convId === c.id ? "bg-indigo-100" : "hover:bg-gray-100"}`}>
                <button onClick={() => openConversation(c)} title={`${c.folderName} — ${c.title}`}
                  className="w-full flex items-start gap-2 text-left px-2 py-1.5 pr-7">
                  <MessageSquare className="w-3.5 h-3.5 shrink-0 mt-0.5 text-gray-400" />
                  <span className="min-w-0 flex-1">
                    <span className={`block text-sm truncate ${convId === c.id ? "text-indigo-800 font-medium" : "text-gray-800 font-medium"}`}>{c.title}</span>
                    <span className="block text-[11px] text-gray-400 truncate">
                      {c.projectId && projById[c.projectId] ? projById[c.projectId].name : "General"}
                    </span>
                  </span>
                </button>
                <button onClick={(e) => deleteConversation(c, e)} title="Delete conversation"
                  className="absolute right-1 top-1.5 p-1 rounded text-gray-400 opacity-0 group-hover:opacity-100 hover:text-red-600 hover:bg-white/60 transition">
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* Chat column */}
        <div className="flex-1 flex flex-col min-h-0 relative">
        <div ref={chatScrollRef} onScroll={onChatScroll} className="flex-1 overflow-y-auto overflow-x-hidden px-6 py-8 leading-5">
          {!chat || chat.messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full gap-4 text-center">
              <div className="w-16 h-16 rounded-full bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center shadow-lg">
                <Briefcase size={28} className="text-white" />
              </div>
              <div>
                <p className="text-lg font-semibold text-gray-800">Your local AI office assistant</p>
                <p className="text-sm text-gray-400 mt-1 max-w-sm">
                  Read documents, draft content, build Word/Excel/PowerPoint files, and use your connected apps — Outlook, Teams, Jira, Confluence. No folder needed; office work runs through your connectors and documents.
                </p>
              </div>
              <div className="flex flex-wrap gap-2 justify-center max-w-md mt-2">
                {SUGGESTIONS.map((s, i) => (
                  <button key={i} onClick={() => sendChat(s.text)}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 text-gray-600 rounded-full border border-gray-200 transition cursor-pointer">
                    <s.icon className="w-3.5 h-3.5 text-gray-400 shrink-0" />{s.text}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              {chat.messages.map((m, i) => (
                <MessageRow key={i} m={m}
                  isLast={m.role === "assistant" && i === chat.messages.length - 1}
                  busy={chatBusy} onRegenerate={regenerate} />
              ))}
              {chat?.statusLine && (
                <div className="flex items-center gap-2 text-xs text-gray-500 mb-6"><BrandMark className="w-4 h-4 brand-breathe shrink-0" />{chat.statusLine}</div>
              )}
            </>
          )}
          {/* Jump to latest — pinned to the chat area's bottom-right while scrolled up. */}
          {!atBottom && chat && chat.messages.length > 0 && (
            <div className="sticky bottom-0 flex justify-end pointer-events-none -mb-4">
              <button
                onClick={() => scrollToBottom("smooth")}
                title="Jump to latest"
                className="pointer-events-auto flex items-center gap-1.5 px-3 py-1.5 rounded-full
                           bg-white border border-gray-300 shadow-md text-gray-600 hover:bg-gray-50
                           text-xs cursor-pointer">
                <ArrowDown size={14} /> Jump to latest
              </button>
            </div>
          )}
        </div>
        <div className="border-t border-gray-100 bg-white px-4 pb-3 pt-3 shrink-0">
          <PermissionBar conv={chat} onAnswer={onPermissionAnswer} />
          <div className="relative">
            {/* Completion menu — "/" commands or (when a folder is open) "@" files. */}
            {compOpen && (
              <div className="absolute bottom-full mb-1 left-0 w-[28rem] max-h-72 overflow-y-auto bg-white border border-gray-200 rounded-lg shadow-lg z-10">
                {compItems.map((c, i) => (
                  <button key={c.label} ref={i === compIdx ? compItemRef : null}
                    onMouseDown={(e) => { e.preventDefault(); applyCompletion(c); }}
                    onMouseEnter={() => setCompIdx(i)}
                    className={`w-full text-left px-3 py-1.5 flex items-baseline gap-2 ${i === compIdx ? "bg-indigo-50" : "hover:bg-gray-50"}`}>
                    <span className="text-xs font-mono text-indigo-600 shrink-0">{c.label}</span>
                    {c.hint && <span className="text-[10px] font-mono text-gray-300 shrink-0">{c.hint}</span>}
                    {c.desc && <span className="text-[11px] text-gray-400 truncate">{c.desc}</span>}
                  </button>
                ))}
              </div>
            )}
            {fileLimitError && (
              <div className="mb-2 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                <span className="mt-0.5 shrink-0">⚠️</span>
                <span className="flex-1">
                  The number of files you are trying to add exceeds the maximum limit. Ainxt currently supports adding up to 5 files at a time.
                </span>
                <button
                  onClick={() => setFileLimitError(false)}
                  className="shrink-0 text-red-400 hover:text-red-600"
                  aria-label="Dismiss"
                >
                  <X size={13} />
                </button>
              </div>
            )}
            {imageBlockedError && (
              <div className="mb-2 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                <span className="mt-0.5 shrink-0">⚠️</span>
                <span className="flex-1">
                  Image files can't be attached in Buddy chat. Supported types: Word, Excel, PowerPoint, PDF, and text/data files.
                </span>
                <button
                  onClick={() => setImageBlockedError(false)}
                  className="shrink-0 text-red-400 hover:text-red-600"
                  aria-label="Dismiss"
                >
                  <X size={13} />
                </button>
              </div>
            )}
            {/* ── Queue limit reached notice ────────────────────────────────── */}
            {isQueueFull && (
              <div className="mb-1 flex items-center gap-2 px-3 py-2 rounded-lg border border-red-200 bg-red-50 text-xs text-red-700">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
                  <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                </svg>
                <span>Queue limit reached ({queuedCount}/{maxWait}). Please wait for the current response to finish before sending more messages.</span>
              </div>
            )}
            {/* ── Prompt queue indicator ─────────────────────────────────────── */}
            {queuedCount > 0 && (
              <div className="mb-1 rounded-lg border border-amber-200 bg-amber-50 text-xs text-amber-700">
                {/* Header row */}
                <div className="flex items-center gap-2 px-3 py-1.5">
                  <svg className="w-3 h-3 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span className="flex-1">
                    {queuedCount} message{queuedCount > 1 ? "s" : ""} queued — will send automatically
                  </span>
                  <button
                    onClick={() => setQueueExpanded((e) => !e)}
                    className="text-amber-600 hover:text-amber-800 font-medium"
                    title={queueExpanded ? "Collapse queue list" : "Show queued messages"}
                  >
                    {queueExpanded ? "▴ Hide" : "▾ Show"}
                  </button>
                  <button
                    onClick={() => { clearQueue(); setQueuedCount(0); setQueueExpanded(false); }}
                    className="text-amber-500 hover:text-amber-700 font-medium"
                    title="Clear all queued messages"
                  >
                    Clear all
                  </button>
                </div>
                {/* Expanded list — each queued message with a delete button */}
                {queueExpanded && (
                  <ul className="border-t border-amber-200 divide-y divide-amber-100">
                    {getQueue().map((item, idx) => (
                      <li key={item.timestamp} className="flex items-center gap-2 px-3 py-1.5">
                        <span className="text-amber-400 font-mono w-4 shrink-0 select-none">{idx + 1}.</span>
                        <span className="flex-1 truncate text-amber-800" title={item.text}>
                          {item.text.length > 60 ? item.text.slice(0, 60) + "…" : item.text}
                        </span>
                        <button
                          onClick={() => {
                            const removed = removeAt(idx);
                            if (removed) {
                              const newCount = queuedCount - 1;
                              setQueuedCount(newCount);
                              if (newCount === 0) setQueueExpanded(false);
                            }
                          }}
                          className="text-amber-400 hover:text-red-500 shrink-0"
                          title="Remove this message from queue"
                        >
                          ✕
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            <div className={`border rounded-xl bg-gray-50 transition-colors ${chatBusy ? "border-gray-200" : "border-gray-300 focus-within:border-gray-400 focus-within:bg-white"}`}>
              {/* Attached-file chips */}
              {attachedFiles.length > 0 && (
                <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
                  {attachedFiles.map((f) => (
                    <span key={f.name} title={f.path}
                      className="inline-flex items-center gap-1 max-w-[16rem] px-2 py-1 rounded-md bg-indigo-50 border border-indigo-200 text-indigo-700 text-xs">
                      <Paperclip size={12} className="shrink-0" />
                      <span className="truncate">{f.name}</span>
                      <button onClick={() => clearAttachment(f.name)} className="ml-0.5 text-indigo-400 hover:text-indigo-700 cursor-pointer"><X size={12} /></button>
                    </span>
                  ))}
                </div>
              )}
              {attaching && (
                <div className="mb-1 flex items-center gap-2 px-3 pt-2">
                  <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
                    {attachPct === null ? (
                      <div className="h-full w-2/5 bg-indigo-500 rounded-full animate-upload-indeterminate" />
                    ) : (
                      <div
                        className="h-full bg-indigo-500 rounded-full transition-all duration-200"
                        style={{ width: `${attachPct}%` }}
                      />
                    )}
                  </div>
                  <span className="text-xs text-indigo-600 font-medium flex items-center gap-1 shrink-0">
                    <Loader2 size={12} className="animate-spin" />
                    {attachPct === null ? "Attaching files…" : `Uploading… ${attachPct}%`}
                  </span>
                </div>
              )}
              <textarea ref={textareaRef} value={chatInput}
                onChange={(e) => { setChatInput(e.target.value); setCompIdx(0); setCompDismissed(false); }}
                onKeyDown={onInputKeyDown}
                placeholder={isQueueFull ? "Queue limit reached — waiting for response…" : "Ask Buddy to read, draft, build a document, or use your apps…"}
                rows={3}
                disabled={isQueueFull}
                className={`w-full resize-none bg-transparent px-3 pt-3 pb-1 outline-none text-sm placeholder-gray-400 ${isQueueFull ? "text-gray-400 cursor-not-allowed" : "text-gray-800"}`} />
              {/* Hidden file input — gives us real File objects with bytes in memory.
                  In Electron, File.path is the absolute disk path. This lets us
                  upload to /chat/upload (server parses) on ANY exe version.
                  Image extensions (.png/.jpg/.jpeg/.gif/.webp/.bmp) are deliberately
                  EXCLUDED here — Buddy's paperclip attach button does not support
                  image uploads. This is Buddy-only; other chat surfaces are
                  unaffected. A hard guard in handleFileInputChange() re-checks
                  this even if the OS "All Files" picker lets one through. */}
              <input
                ref={fileInputRef}
                type="file"
                multiple
                style={{ display: "none" }}
                onChange={handleFileInputChange}
                accept=".docx,.xlsx,.xls,.xlsm,.pdf,.pptx,.ppt,.odt,.ods,.rtf,.html,.htm,.csv,.txt,.md,.json,.xml,.yaml,.yml"
              />
              <div className="flex items-center gap-1 px-2 pb-2">
                <button onClick={attachFile} disabled={attaching}
                  title={attaching ? "Attaching files…" : "Attach a file for Buddy to read"}
                  className="p-1.5 cursor-pointer text-gray-400 hover:text-indigo-600 transition disabled:opacity-40">
                  {attaching ? <Loader2 size={18} className="animate-spin text-indigo-500" /> : <Paperclip size={18} />}
                </button>
                <div className="flex-1" />
                <button onClick={chatBusy ? () => coworkOfficeInterrupt(chatId) : () => sendChat()} disabled={chatSessionPending || (!chatBusy && !chatInput.trim()) || isQueueFull}
                  title={chatSessionPending ? "Loading session…" : isQueueFull ? `Queue limit reached (${queuedCount}/${maxWait})` : undefined}
                  className="p-1.5 cursor-pointer text-gray-500 hover:text-gray-800 transition disabled:opacity-30">
                  {chatSessionPending ? <Loader2 size={20} className="animate-spin text-indigo-400" /> : chatBusy ? <CirclePauseIcon size={20} /> : <SendHorizontal size={20} />}
                </button>
              </div>
            </div>
          </div>
          {/* Status bar: model · permission mode · context · session cost */}
          <div className="flex items-center gap-3 mt-1.5 px-1 text-[11px] text-gray-400">
            {roles.length > 0 && (
              <>
                <label className="flex items-center gap-1 cursor-pointer hover:text-gray-600" title="Role / plugin — specialise Buddy for a job">
                  <Briefcase className="w-3 h-3" />
                  <select value={roleId} onChange={(e) => onRoleChange(e.target.value)}
                    className="bg-transparent outline-none cursor-pointer text-gray-500 hover:text-gray-700">
                    <option value="">Buddy (general)</option>
                    {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                  </select>
                </label>
                <span className="text-gray-200">|</span>
              </>
            )}
            {/* Model display is fully HIDDEN in Buddy when locked (no picker, no
                label, no separator). Only shown when the lock is off (switching
                allowed). */}
            {!modelLocked && (
              <>
                <label className="flex items-center gap-1 cursor-pointer hover:text-gray-600" title="Model (or type /model)">
                  <Cpu className="w-3 h-3" />
                  <select value={model} onChange={(e) => onModelChange(e.target.value)} onFocus={refreshModelLists}
                    className="bg-transparent outline-none cursor-pointer text-gray-500 hover:text-gray-700">
                    {/* Grouped by provider (Claude/OpenAI/Google/Local) when sourced
                        from /all-models; a flat list (no `provider` field) falls back to
                        plain <option>s — covers the BASE_MODELS pre-load state. */}
                    {(() => {
                      const groups = new Map();
                      for (const m of models) {
                        const key = m.provider || "";
                        if (!groups.has(key)) groups.set(key, []);
                        groups.get(key).push(m);
                      }
                      return [...groups.entries()].map(([provider, list]) => {
                        const opts = list.map((m) => (
                          <option key={m.key} value={m.key}>
                            {m.label}{m.tier ? ` · ${m.tier === "paid" ? "Paid" : "Free"}` : ""}
                          </option>
                        ));
                        return provider
                          ? <optgroup key={provider} label={provider}>{opts}</optgroup>
                          : opts;
                      });
                    })()}
                  </select>
                </label>
                <span className="text-gray-200">|</span>
              </>
            )}
            <label className="flex items-center gap-1 cursor-pointer hover:text-gray-600" title="Permission mode — connector sends + document writes always confirm">
              <ShieldCheck className="w-3 h-3" />
              <select value={permMode} onChange={(e) => onPermModeChange(e.target.value)}
                className="bg-transparent outline-none cursor-pointer text-gray-500 hover:text-gray-700">
                {PERM_MODES.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
              </select>
            </label>
            <div className="flex-1" />
            {ctxPct != null && (
              <span className="flex items-center gap-1 font-mono" title={`${fmtTok(chat?.contextTokens)} / ${fmtTok(chat?.contextMax)} tokens in context`}>
                <Gauge className="w-3 h-3" />{ctxPct}% ctx
              </span>
            )}
            {typeof sessionCost === "number" && (
              <span className="font-mono" title="Total cost this conversation">{fmtCost(sessionCost)} session</span>
            )}
          </div>
        </div>
        </div>
      </div>
    </div>
  );
}
