// SPDX-License-Identifier: MIT
/* Code — local-agent (engineer) mode. AiNxt's agentic coding environment.
 *
 * Runs the FULL ainxt agent ON THE USER'S MACHINE via the local CLI (spawned by
 * the desktop main process, driven over the NDJSON --stream-json protocol). It
 * opens a chosen local repo, edits local files, runs commands, and holds a
 * multi-turn conversation — the "AI software engineer" model.
 *
 * NOTE: the desktop IPC namespace remains `window.ainxtDesktop.cowork.*` and the
 * sidebar view id remains "cowork" — only the UI label is "Code". The IPC id was
 * intentionally NOT renamed to avoid churn across main.js/preload.js/cliManager.
 *
 * Desktop-only: gated behind `isCoworkAvailable` (window.ainxtDesktop.cowork). In
 * a browser it renders an explanatory placeholder. All CLI I/O goes through the
 * helpers in hooks/useDesktop.js — never direct process/fs access.
 */
import { useEffect, useReducer, useRef, useState, useCallback, useMemo, memo } from "react";
import {
  FolderOpen, SendHorizontal, CirclePauseIcon, Terminal, FileDiff, Loader2,
  ShieldQuestion, CheckCircle2, XCircle, Cpu, MonitorSmartphone, Plus, MessageSquare, Trash2,
  Gauge, ShieldCheck, Pencil, Map as MapIcon,
  Copy, Check, Volume2, VolumeX, RotateCcw, GitBranch, X,
  FolderTree, Columns2, SquareStack, FileText, Paperclip, Sun, Moon,
  Mic, MicOff, ImageIcon,
} from "lucide-react";
import {
  isCoworkAvailable, coworkAuthState, coworkLogin, coworkOnLoginOutput,
  coworkCreateSession, coworkRun, coworkRespondConfirm, coworkInterrupt,
  coworkOnEvent, coworkAdoptToken, coworkHasValidKey, coworkOnAuthUpdated,
  pickFolder, pickFile, listFolder,
  coworkSetModel, coworkSetPermissionMode, coworkClone,
  readFile, createPath, deletePath, renamePath,
  watchFolder, unwatchFolder, onWorkspaceChange, offWorkspaceChange,
  openExternal,
} from "../hooks/useDesktop.js";
import MessageMeta from "./MessageMeta.jsx";
import BrandMark from "./BrandMark.jsx";
import AiNxtSpinner from "./AiNxtSpinner.jsx";
import DiffLines from "./code/DiffLines.jsx";
import FileExplorer from "./code/FileExplorer.jsx";
import FileEditorPanel from "./code/FileEditorPanel.jsx";
import { API_BASE, authFetch, MODEL_DEFAULT, MODEL_ALIASES, MODEL_PICKER } from "../config";

// Code task sessions are SERVER-persisted (Postgres /code/conversations) — NO
// localStorage (it's lost on app restart). Scoped to the JWT user + project
// folder, in a table SEPARATE from Buddy chats so the two histories never mix.

function _folderName(folder) { return folder ? (folder.split("/").filter(Boolean).pop() || folder) : "Project"; }
async function convListAll() {
  try {
    const r = await authFetch(`${API_BASE}/code/conversations`);
    const d = await r.json();
    return (d?.conversations || []).map((c) => ({
      id: c.id, folder: c.folder || null, folderName: _folderName(c.folder),
      title: c.title || "Conversation",
      createdAt: c.created_at ? Date.parse(c.created_at) : 0,
      updatedAt: c.updated_at ? Date.parse(c.updated_at) : 0,
    })).sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
  } catch { return []; }
}
async function convGetFull(id) {
  try { const r = await authFetch(`${API_BASE}/code/conversations/${id}`); return r.ok ? await r.json() : null; }
  catch { return null; }
}
// Strip transient streaming flags so a conversation saved mid-stream doesn't
// reopen stuck on "Thinking…" — and settle any tool/diff block left "running"
// (refresh or abandoned turn) so its spinner doesn't spin forever.
function _sanitize(messages) {
  return (messages || []).map((m) => ({
    ...m,
    streaming: false,
    blocks: (m.blocks || []).map((b) => (b.status === "running" ? { ...b, status: "done" } : b)),
  }));
}
// Upsert / delete to the server (fire-and-forget — never blocks a turn).
function convSave(folder, conv) {
  if (!conv?.id) return;
  authFetch(`${API_BASE}/code/conversations/${conv.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: conv.title || "Conversation", messages: _sanitize(conv.messages), folder: folder || null }),
  }).catch(() => {});
}
function convDelete(id) {
  if (!id) return;
  authFetch(`${API_BASE}/code/conversations/${id}`, { method: "DELETE" }).catch(() => {});
}
function convRename(id, title) {
  if (!id || !title?.trim()) return;
  authFetch(`${API_BASE}/code/conversations/${id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title.trim().slice(0, 200), messages: [], folder: null }),
  }).catch(() => {});
}
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import { mdComponents } from "./Message.jsx";

// Context-window badge for the model picker (same as Chat.jsx).
const MODEL_CONTEXT_BADGE = [
  ["gemini", "1M"],
  ["gpt-5",  "256K"],
  ["gpt",    "128K"],
  ["claude", "200K"],
  ["sonnet", "200K"],
  ["opus",   "200K"],
  ["haiku",  "200K"],
  ["kimi",   "128K"],
  ["local",  "128K"],
];
function _modelContextBadge(value = "", label = "") {
  const hay = `${value} ${label}`.toLowerCase();
  for (const [key, tag] of MODEL_CONTEXT_BADGE) {
    if (hay.includes(key)) return tag;
  }
  return null;
}
function _modelTierTag(tier) {
  if (tier === "paid") return "Paid";
  if (tier === "free") return "Free";
  return null;
}
// Module-level label resolver (fallback only — reducer uses this outside the component).
// The component's MODEL_OPTIONS has richer labels; this is just a fallback for the reducer.
const MODEL_LABEL = (id) => (id || "").replace(/^claude-/, "") || "model";

// Permission modes the agent accepts via set_permission_mode.
const PERM_MODES = [
  { key: "default",          label: "Ask each time", icon: ShieldCheck },
  { key: "acceptEdits",      label: "Auto-accept edits", icon: Pencil },
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

// ── Conversation reducer ──────────────────────────────────────────────────────
// One entry per CLI session (the chat + each background task). Events from the
// CLI mutate the latest assistant message's blocks; result finalizes the turn.

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

// Normalize a CLI/watcher path to a "/"-relative path inside `folder`. The CLI
// runs with cwd=folder, so diff paths arrive either absolute or already relative.
function normalizeRel(p, folder) {
  if (!p) return "";
  let s = String(p).replace(/\\/g, "/");
  const f = String(folder || "").replace(/\\/g, "/").replace(/\/+$/, "");
  if (f && s.startsWith(f + "/")) s = s.slice(f.length + 1);
  return s.replace(/^\.\//, "").replace(/^\/+/, "");
}

function applyEvent(conv, ev) {
  const a = lastAssistant(conv);
  switch (ev.type) {
    case "token":   if (a) appendText(a.blocks, ev.text); break;
    case "newline": if (a) appendText(a.blocks, "\n"); break;
    // 'line' is the history-only mirror of streamed tokens — ignored for display
    // (we reconcile to result.response at the end). See store.ts.
    case "line": break;
    case "tool:start":
      if (a) a.blocks.push({ kind: "tool", name: ev.name, detail: ev.detail, status: "running", diff: ev.diff || null });
      break;
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
    case "notice":
      if (a && ev.level === "warn") a.blocks.push({ kind: "notice", msg: ev.msg, level: "warn" });
      break;
    case "error":
      if (a) a.blocks.push({ kind: "notice", msg: ev.msg, level: "error" });
      break;
    case "phase":      conv.statusLine = `Phase: ${ev.phase}`; break;
    case "agent:iter": conv.statusLine = `Working — step ${ev.iter}/${ev.max} (${ev.phase})`; break;
    case "agent:ttfb": conv.statusLine = "Thinking…"; break;
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
        // The initialize handshake carries descriptions; the later system/init
        // carries names only — don't let the bare list overwrite the rich one.
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
            // drop any other stray text blocks created during streaming
            a.blocks = a.blocks.filter((b, i) => b.kind !== "text" || i === idx);
          } else {
            a.blocks.unshift({ kind: "text", text: ev.response });
          }
        }
        if (ev.status === "error") a.blocks.push({ kind: "notice", msg: ev.error || "Run failed", level: "error" });
        a.streaming = false;
        // Settle any tool/diff block still "running" (e.g. interrupted turn) so it
        // doesn't keep spinning after the turn ends.
        a.blocks.forEach((b) => { if (b.status === "running") b.status = ev.status === "error" ? "fail" : "done"; });
        // Per-iteration cost + tokens + duration (shown under the message).
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
    case "session:exit": {
      conv.status = "exited"; conv.statusLine = "";
      const la = lastAssistant(conv);
      if (la) { la.streaming = false; la.blocks.forEach((b) => { if (b.status === "running") b.status = "done"; }); }
      break;
    }
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
        statusLine: "",
        messages: [
          ...conv.messages,
          { role: "user", blocks: [{ kind: "text", text: action.text }], attachments: action.attachments || [] },
          { role: "assistant", blocks: [], streaming: true },
        ],
      };
      return { convs: { ...state.convs, [action.id]: next } };
    }
    case "TRUNCATE_TO": {
      // Drop all messages from `count` onward (used by regenerate — remove the
      // target assistant reply and everything after it before re-asking).
      const conv = state.convs[action.id];
      if (!conv) return state;
      const next = { ...conv, messages: conv.messages.slice(0, action.count) };
      return { convs: { ...state.convs, [action.id]: next } };
    }
    case "EVENT": {
      const conv = state.convs[action.id];
      if (!conv) return state;
      // shallow-clone the conv + messages so React re-renders
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
function ToolChip({ b }) {
  const Icon = b.status === "running" ? Loader2 : b.status === "fail" ? XCircle : CheckCircle2;
  const color = b.status === "running" ? "text-blue-600" : b.status === "fail" ? "text-red-600" : "text-emerald-600";
  return (
    <div className="flex items-center gap-2 text-xs px-2 py-1 font-mono">
      <Icon className={`w-3.5 h-3.5 shrink-0 ${color} ${b.status === "running" ? "animate-spin" : ""}`} />
      <span className="text-gray-700 shrink-0">{b.name}</span>
      {b.detail && <span className="text-gray-400 truncate">· {b.detail}</span>}
    </div>
  );
}

// Collapsible panel for a run of consecutive tool calls — keeps the answer
// prominent. Bounded-height scroll when expanded; one-line summary when collapsed.
function ToolGroup({ items }) {
  const running = items.some((t) => t.status === "running");
  // expanded while the agent is actively working; user can toggle anytime.
  const [open, setOpen] = useState(true);
  // Header reflects only progress (running vs done) — an individual tool that
  // failed (e.g. a path guess the agent then corrected) keeps its own ✗ row and
  // shouldn't make the whole group look failed.
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
      <DiffLines lines={b.lines} className="max-h-72 overflow-y-auto" />
    </div>
  );
}

// A mutating tool (Edit/Write/MultiEdit) shown as a diff card with apply status.
// Collapsible; defaults open while running so the user sees the proposed change
// next to the permission prompt, then can collapse once applied.
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
        <DiffLines lines={d.lines} truncated={d.truncated} className="max-h-80 overflow-y-auto" />
      )}
    </div>
  );
}

function Blocks({ blocks }) {
  // Coalesce consecutive tool calls into one collapsible ToolGroup so a turn
  // with many file reads doesn't push the answer off-screen.
  const out = [];
  let i = 0;
  while (i < blocks.length) {
    const b = blocks[i];
    // A mutating tool with a diff renders as its own diff card (with apply
    // status); read-only tools coalesce into a collapsible group.
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
      if (b.text) out.push(
        <div key={i} className="md-body">
          <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeHighlight, rehypeKatex]} components={mdComponents}>
            {b.text}
          </ReactMarkdown>
        </div>
      );
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
    }
    i++;
  }
  return <>{out}</>;
}

const MessageRow = memo(function MessageRow({ m, index, onRegenerate, busy, statusLine }) {
  const [copied, setCopied] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  if (m.role === "user") {
    return (
      <div className="flex flex-col items-end mb-6 gap-1.5">
        <div className="bg-gray-100 px-4 py-3 rounded-md text-sm max-w-4xl whitespace-pre-wrap text-gray-800">
          {m.blocks[0]?.text}
        </div>
        {m.attachments?.length > 0 && (
          <div className="flex flex-wrap gap-1.5 justify-end max-w-4xl">
            {m.attachments.map((a) => (
              <span key={a.path} title={a.path}
                className="inline-flex items-center gap-1 max-w-[16rem] text-xs bg-indigo-50 text-indigo-700 border border-indigo-100 rounded-md px-1.5 py-0.5">
                {a.isImage ? <ImageIcon size={12} className="shrink-0" /> : <Paperclip size={12} className="shrink-0" />}
                <span className="truncate">{a.name}</span>
              </span>
            ))}
          </div>
        )}
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
  return (
    <div className="flex justify-start mb-6 gap-2.5">
      <div className="w-7 h-7 rounded-full brand-grad-vivid flex items-center justify-center shrink-0 shadow-sm mt-0.5">
        <BrandMark className="w-4 h-4" alt="AiNxt" />
      </div>
      <div className="px-0 py-1 rounded-md text-sm min-w-0 flex-1 max-w-4xl text-gray-800">
        <Blocks blocks={m.blocks} />
        {/* Action bar + meta pills (Chat-equivalent) — assistant, once settled. */}
        {!m.streaming && (m.blocks?.length > 0) && (
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
              <button onClick={() => onRegenerate?.(index)} disabled={busy} title="Regenerate this response"
                className="p-1 rounded cursor-pointer text-gray-600 hover:text-purple-500 transition-colors ml-0.5 disabled:opacity-30">
                <RotateCcw size={14} />
              </button>
            </div>
            <MessageMeta msg={{
              role: "assistant", streaming: false,
              modelLabel: m.modelLabel,
              inTok: c?.input ?? null, outTok: c?.output ?? null,
              costUsd: c?.usd ?? null,
              latency: c?.elapsedMs ? c.elapsedMs / 1000 : null,
            }} />
          </>
        )}
        {m.streaming && (
          <div className="mt-2">
            <AiNxtSpinner label={statusLine || null} outTok={m.cost?.output ?? null} />
          </div>
        )}
      </div>
    </div>
  );
});

// Inline permission strip — shown just above the input box (never a modal overlay).
function PermissionBar({ conv, onAnswer }) {
  if (!conv?.pendingConfirm) return null;
  const { id, tool, detail } = conv.pendingConfirm;
  const sid = conv.id;
  return (
    <div className="mb-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2">
      <div className="flex items-center gap-2 min-w-0">
        <ShieldQuestion className="w-4 h-4 shrink-0 text-amber-600" />
        <span className="text-sm text-gray-800 min-w-0">
          <span className="font-medium">{tool || "Tool"}</span>
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
export default function Code() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [folder, setFolder] = useState(null);
  const [model, setModel] = useState(MODEL_DEFAULT);
  const [permMode, setPermMode] = useState("default");
  // Dynamic model list from backend (same pattern as Chat.jsx)
  const [allModelProviders, setAllModelProviders] = useState([]);
  const [allowedModels, setAllowedModels] = useState([]);
  const [governanceLoaded, setGovernanceLoaded] = useState(false);
  const MODEL_OPTIONS = (() => {
    const raw = allModelProviders.length > 0
      ? allModelProviders.flatMap((group, gi) => [
          ...(gi > 0 ? [{ value: `__div_${gi}__`, label: `── ${group.provider} ──`, disabled: true }] : []),
          ...group.models.map(m => ({ value: m.id, modelId: m.modelId || m.id, label: m.label, tier: m.tier })),
        ])
      : MODEL_PICKER.map(m => ({ value: m.key, modelId: m.key, label: m.label }));
    if (!governanceLoaded) return raw;
    return raw.filter(o =>
      o.disabled ||
      allowedModels.includes(o.modelId || o.value)
    );
  })();
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
  const [isListening, setIsListening] = useState(false);  // voice (STT) active
  const recognitionRef = useRef(null);                    // Web Speech API instance
  const [conversations, setConversations] = useState([]); // desktop-saved history for the folder
  const [editingTitle, setEditingTitle] = useState(null);  // conversation id being renamed
  const [editTitle, setEditTitle] = useState("");
  const [files, setFiles] = useState([]);               // repo file list (relative paths) for @-mentions
  const [compIdx, setCompIdx] = useState(0);            // highlighted completion-menu row
  const [compDismissed, setCompDismissed] = useState(false); // Esc closed the menu until next edit
  const chatScrollRef = useRef(null);
  const textareaRef = useRef(null);
  const compItemRef = useRef(null);                     // active completion row (for scrollIntoView)
  const folderRef = useRef(null);
  const convIdRef = useRef(null);
  const chatIdRef = useRef(null);
  const convsRef = useRef(state.convs);
  const openingRef = useRef(false);   // true while opening a conv (suppress folder-change reset)
  const primedRef = useRef(new Set()); // session ids that already got the working-dir context
  useEffect(() => { folderRef.current = folder; }, [folder]);
  useEffect(() => { convIdRef.current = convId; }, [convId]);
  useEffect(() => { chatIdRef.current = chatId; }, [chatId]);
  useEffect(() => { convsRef.current = state.convs; }, [state.convs]);

  // ── Lite IDE: explorer + editor panel ──────────────────────────────────────
  const [layoutMode, setLayoutMode] = useState("tabbed"); // "tabbed" (editor replaces chat) | "split" (beside chat)
  const [centerTab, setCenterTab] = useState("chat");      // tabbed mode: "chat" | <rel>
  const layoutModeRef = useRef(layoutMode);
  useEffect(() => { layoutModeRef.current = layoutMode; }, [layoutMode]);
  const [railTab, setRailTab] = useState("chats");        // "files" | "chats"
  const [openFiles, setOpenFiles] = useState([]);          // rel paths open as tabs
  const [activeFile, setActiveFile] = useState(null);      // rel path of focused tab
  const [viewerMode, setViewerMode] = useState("edit");    // "edit" | "diff"
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelWidth, setPanelWidth] = useState(560);
  const [changedFiles, setChangedFiles] = useState(() => new Map()); // rel → {added,removed,isNew,lines,truncated}
  const [reloadSignal, setReloadSignal] = useState(0);     // watcher → editor buffer refresh
  const [reloadTarget, setReloadTarget] = useState(null);
  const [refreshing, setRefreshing] = useState(false);     // manual disk re-scan in flight
  const [refreshErr, setRefreshErr] = useState("");        // last disk-read error (surfaced, not swallowed)
  // Dark High-Contrast theme, scoped to the Code panel only (persisted).
  const [dark, setDark] = useState(() => {
    try { return localStorage.getItem("ainxt.code.theme") === "dark"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem("ainxt.code.theme", dark ? "dark" : "light"); } catch { /* ignore */ }
  }, [dark]);
  const openFileRef = useRef(null);     // latest openFile (for the []-deps event handler)
  const refreshFilesRef = useRef(null); // latest refreshFiles (for the mount-once watcher)
  const changedFilesRef = useRef(changedFiles); // current changedFiles for the []-deps handler
  const absOfRef = useRef(null);        // latest absOf (for the []-deps event handler)
  const watchDebounceRef = useRef(null);
  useEffect(() => { changedFilesRef.current = changedFiles; }, [changedFiles]);

  const sep = useMemo(() => (folder && /\\/.test(folder) ? "\\" : "/"), [folder]);
  const absOf = useCallback((rel) => {
    if (!folder) return rel;
    if (rel.startsWith("/") || /^[A-Za-z]:[\\/]/.test(rel)) return rel; // already absolute
    const base = folder.endsWith(sep) ? folder.slice(0, -1) : folder;
    return base + sep + String(rel).split("/").join(sep);
  }, [folder, sep]);
  useEffect(() => { absOfRef.current = absOf; }, [absOf]);

  // open a file as a tab. Split mode → show the right panel; tabbed mode → make
  // it the center tab (so an agent edit's diff comes up, replacing the chat).
  const openFile = useCallback((rel, mode) => {
    setOpenFiles((o) => (o.includes(rel) ? o : [...o, rel]));
    setActiveFile(rel);
    setViewerMode(mode || "edit");
    if (layoutModeRef.current === "split") setPanelOpen(true);
    else setCenterTab(rel);
  }, []);
  useEffect(() => { openFileRef.current = openFile; }, [openFile]);

  const closeTab = useCallback((rel) => {
    setOpenFiles((o) => {
      const idx = o.indexOf(rel);
      const next = o.filter((r) => r !== rel);
      const fallback = next.length ? next[Math.min(idx, next.length - 1)] : null;
      setActiveFile((a) => (a !== rel ? a : fallback));
      setCenterTab((c) => (c !== rel ? c : (fallback || "chat")));
      return next;
    });
  }, []);

  // Split mode needs the right panel visible once a file is open.
  useEffect(() => { if (layoutMode === "split" && activeFile) setPanelOpen(true); }, [layoutMode, activeFile]);

  const refreshFiles = useCallback(() => {
    const f = folderRef.current;
    if (!f) { setFiles([]); return; }
    setRefreshing(true);
    setRefreshErr("");
    listFolder(f, { maxFiles: 4000 }).then((list) => {
      setFiles((list || []).map((x) => normalizeRel(x.path, f) || x.name));
    }).catch((err) => {
      // Don't swallow — surface so a failed disk read is visible, not silent.
      const msg = err?.message || String(err) || "Could not read the folder from disk.";
      console.warn("[Code] refreshFiles failed:", err);
      setRefreshErr(msg);
    }).finally(() => setRefreshing(false));
  }, []);
  useEffect(() => { refreshFilesRef.current = refreshFiles; }, [refreshFiles]);

  const onCreateFile = useCallback(async (rel, isDir) => {
    const res = await createPath(absOf(rel), isDir);
    if (res?.ok) { refreshFiles(); if (!isDir) openFile(rel, "edit"); }
    else if (res?.error) window.alert(res.error);
  }, [absOf, refreshFiles, openFile]);

  const onRenameFile = useCallback(async (oldRel, newRel) => {
    const res = await renamePath(absOf(oldRel), absOf(newRel));
    if (!res?.ok) { if (res?.error) window.alert(res.error); return; }
    refreshFiles();
    setOpenFiles((o) => o.map((r) => (r === oldRel || r.startsWith(oldRel + "/") ? newRel + r.slice(oldRel.length) : r)));
    setActiveFile((a) => (a === oldRel || a?.startsWith(oldRel + "/") ? newRel + a.slice(oldRel.length) : a));
    setChangedFiles((prev) => { if (!prev.has(oldRel)) return prev; const m = new Map(prev); m.set(newRel, m.get(oldRel)); m.delete(oldRel); return m; });
  }, [absOf, refreshFiles]);

  const onDeleteFile = useCallback(async (rel, type) => {
    if (!window.confirm(`Move ${type === "dir" ? "folder" : "file"} "${rel}" to the trash?`)) return;
    const res = await deletePath(absOf(rel));
    if (!res?.ok) { if (res?.error) window.alert(res.error); return; }
    refreshFiles();
    setOpenFiles((o) => o.filter((r) => r !== rel && !r.startsWith(rel + "/")));
    setActiveFile((a) => (a === rel || a?.startsWith(rel + "/") ? null : a));
    setChangedFiles((prev) => { const m = new Map(prev); for (const k of [...m.keys()]) if (k === rel || k.startsWith(rel + "/")) m.delete(k); return m; });
  }, [absOf, refreshFiles]);

  const onSaved = useCallback((rel) => {
    setChangedFiles((prev) => { if (!prev.has(rel)) return prev; const m = new Map(prev); m.delete(rel); return m; });
  }, []);

  const changedSet = useMemo(() => new Set(changedFiles.keys()), [changedFiles]);

  // Drag the splitter between chat and the editor panel.
  const startResize = useCallback((e) => {
    e.preventDefault();
    const startX = e.clientX, startW = panelWidth;
    const onMove = (ev) => setPanelWidth(Math.max(360, Math.min(900, startW + (startX - ev.clientX))));
    const onUp = () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [panelWidth]);

  // Persist the current conversation NOW (uses refs → never stale). Called both
  // by the settle-effect and explicitly on New chat, so a chat is never lost.
  const persistCurrent = useCallback(() => {
    const f = folderRef.current, cid = convIdRef.current, sid = chatIdRef.current;
    if (!f || !cid || !sid) return;
    const conv = convsRef.current[sid];
    if (!conv || !conv.messages?.length) return;
    const firstUser = conv.messages.find((m) => m.role === "user");
    const title = (firstUser?.blocks?.[0]?.text || "Conversation").slice(0, 60);
    convSave(f, { id: cid, title, messages: conv.messages });
  }, []);

  // The sidebar shows ALL conversations across every folder/project, so nothing
  // vanishes when you switch folders.
  const refreshConversations = useCallback(() => { convListAll().then(setConversations); }, []);
  useEffect(() => { refreshConversations(); }, [refreshConversations]); // load on mount

  // ── Dynamic model discovery (same pattern as Chat.jsx) ──────────
  // Extracted so it can be called both on mount AND right before the model
  // dropdown opens (see the <select>'s onFocus below) — otherwise a model an
  // admin adds/syncs via the "LLM Providers" screen while this tab is already
  // open never appears until a full page reload.
  const refreshModelLists = useCallback(() => {
    let alive = true;
    (async () => {
      try {
        const [allR, govR] = await Promise.all([
          authFetch(`${API_BASE}/all-models`),
          authFetch(`${API_BASE}/model-governance/my-models`),
        ]);
        if (!alive) return;
        if (allR.ok) {
          const data = await allR.json();
          if (alive && Array.isArray(data?.providers)) setAllModelProviders(data.providers);
        }
        if (govR.ok) {
          const data = await govR.json();
          if (alive) {
            if (Array.isArray(data?.models)) setAllowedModels(data.models);
            setGovernanceLoaded(!!data?.governance_loaded);
          }
        }
      } catch { /* offline — keep fallback hardcoded list */ }
    })();
    return () => { alive = false; };
  }, []);

  useEffect(() => refreshModelLists(), [refreshModelLists]);

  // Switching folder = switching project. Save the current conversation first,
  // then start fresh — UNLESS the change came from opening a saved conversation
  // (openConversation sets the folder itself and restores that conv).
  useEffect(() => {
    if (openingRef.current) { openingRef.current = false; return; }
    persistCurrent();
    refreshConversations();
    setChatId(null); setConvId(null);
    // Switching project resets the IDE surface.
    setOpenFiles([]); setActiveFile(null); setPanelOpen(false); setCenterTab("chat");
    setChangedFiles(new Map());
  }, [folder]);

  // Load the repo file list ("/"-relative) for @-mention completion + explorer.
  useEffect(() => { refreshFiles(); }, [folder, refreshFiles]);

  // Subscribe to the CLI event stream once.
  useEffect(() => {
    if (!isCoworkAvailable) return;
    const off = coworkOnEvent(({ id, event }) => {
      if (event.type === "session:id") return;
      dispatch({ type: "EVENT", id, event });

      // Sync UI model dropdown when the CLI changes model (e.g. /model in terminal).
      // session:init carries the initial model; result carries the model used for that turn.
      if ((event.type === "session:init" || event.type === "result") && event.model) {
        setModel(event.model);
      }

      // Lite-IDE mirror: agent file edits arrive as a `tool:start` carrying a
      // full diff {path, added, removed, isNew, lines, truncated} (Edit/Write/
      // MultiEdit). Surface them in the explorer + editor and auto-follow.
      const f = folderRef.current;
      if (!f) return;
      if (event.type === "tool:start" && event.diff && event.diff.path) {
        const d = event.diff;
        const rel = normalizeRel(d.path, f);
        if (!rel) return;
        const existing = changedFilesRef.current?.get(rel);
        setChangedFiles((prev) => {
          const m = new Map(prev);
          const cur = m.get(rel) || {};
          m.set(rel, { ...cur, added: d.added, removed: d.removed, isNew: d.isNew, lines: d.lines || [], truncated: d.truncated || 0 });
          return m;
        });
        openFileRef.current?.(rel, "diff"); // auto-follow → show this file's diff
        // Snapshot the pre-edit file ONCE — this is the "before" for the full-file
        // inline diff. For "ask each time" the edit isn't applied yet, so reading
        // now captures the original; new files have an empty original.
        if (!existing || existing.before === undefined) {
          if (d.isNew) {
            setChangedFiles((prev) => { const cur = prev.get(rel); if (!cur) return prev; const m = new Map(prev); m.set(rel, { ...cur, before: "" }); return m; });
          } else {
            readFile(absOfRef.current(rel)).then((res) => {
              if (res?.error) return;
              const before = res?.content ?? "";
              setChangedFiles((prev) => { const cur = prev.get(rel); if (!cur || cur.before !== undefined) return prev; const m = new Map(prev); m.set(rel, { ...cur, before }); return m; });
            });
          }
        }
      }
    });
    return off;
  }, []);

  // Watch the workspace once on mount; refresh the tree + reload the open buffer
  // when files change on disk (agent edits, external edits). Registered once so
  // the preload listener isn't churned on every folder switch.
  useEffect(() => {
    if (!isCoworkAvailable) return;
    const handler = (data) => {
      const f = folderRef.current;
      if (!f || !data?.folder || data.folder !== f) return;
      const rel = normalizeRel(data.filename, f);
      if (!rel) return;
      setReloadTarget(rel); setReloadSignal((n) => n + 1);
      clearTimeout(watchDebounceRef.current);
      watchDebounceRef.current = setTimeout(() => refreshFilesRef.current?.(), 400);
    };
    onWorkspaceChange(handler);
    return () => { offWorkspaceChange(handler); };
  }, []);

  // Start/stop the OS watcher as the folder changes.
  useEffect(() => {
    if (!folder || !isCoworkAvailable) return;
    watchFolder(folder);
    return () => { unwatchFolder(folder); };
  }, [folder]);

  // Persist the active conversation to the desktop store whenever it settles
  // (status change or new message) — never lost on navigate/reload/restart.
  const convStatus = state.convs[chatId]?.status;
  const convLen = state.convs[chatId]?.messages?.length;
  useEffect(() => {
    if (convStatus && convStatus !== "running") { persistCurrent(); refreshConversations(); }
  }, [chatId, convId, folder, convStatus, convLen, persistCurrent, refreshConversations]);

  // New chat: SAVE the current conversation first (so it stays in the list),
  // then clear to start fresh.
  const newChat = useCallback(() => {
    persistCurrent();
    if (chatIdRef.current) coworkInterrupt(chatIdRef.current); // stop the abandoned turn
    refreshConversations();
    setChatId(null); setConvId(null);
  }, [persistCurrent, refreshConversations]);

  // Open a saved conversation: restore its messages for display and start a
  // fresh live session to continue in. (The CLI can't restore agent context, so
  // it re-reads the repo; your transcript stays visible and new turns append.)
  const openConversation = useCallback(async (c) => {
    if (c.id === convIdRef.current) return;        // already open
    persistCurrent();                               // save the conversation we're leaving
    if (chatIdRef.current) coworkInterrupt(chatIdRef.current); // stop the abandoned turn
    const f = c.folder || folderRef.current;
    if (!f) return;
    const saved = await convGetFull(c.id);
    const messages = _sanitize((saved && saved.messages) || []);
    const res = await coworkCreateSession(f);
    if (!res?.id) return;
    if (f !== folderRef.current) { openingRef.current = true; folderRef.current = f; setFolder(f); }
    dispatch({ type: "ADD", conv: { id: res.id, kind: "chat", cwd: f, title: c.title, status: "idle", statusLine: "", messages, pendingConfirm: null } });
    setChatId(res.id);
    setConvId(c.id);
    refreshConversations();
  }, [persistCurrent, refreshConversations]);

  // Delete a saved conversation (and clear the view if it's the active one).
  const deleteConversation = useCallback((c, e) => {
    if (e) e.stopPropagation();
    convDelete(c.id);
    if (c.id === convIdRef.current) { setChatId(null); setConvId(null); }
    refreshConversations();
  }, [refreshConversations]);

  // Reuse the EXISTING web-app session for local code mode — no second sign-in.
  // The renderer is already authenticated via the httpOnly session cookie (which
  // JS can't read). We provision the CLI with a LONG-LIVED API KEY (no sid, no
  // expiry) so Code never hits the session-registry 401 ("CLI login failure")
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
      const cur = await coworkHasValidKey();
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
      const res = await coworkAdoptToken(key, /* isApiKey */ true);
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
    if (!isCoworkAvailable) { setAuth({ authenticated: false, error: "not_desktop" }); return; }
    (async () => {
      try {
        // Trust silentAdopt directly: if the stored key validates against the gateway,
        // the user IS authenticated — no need to re-read config.json (which may be
        // stale or missing on first launch). Only fall back to the file-read if
        // silentAdopt could not confirm a valid key (e.g. network hiccup, no key stored).
        const adopted = await silentAdopt();
        if (adopted.ok) { setAuth({ authenticated: true }); return; }
        const st = await coworkAuthState();
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
    return coworkOnAuthUpdated(({ authenticated }) => {
      if (authenticated) setAuth({ authenticated: true });
    });
  }, []);

  useEffect(() => {
    chatScrollRef.current?.scrollTo({ top: chatScrollRef.current.scrollHeight, behavior: "smooth" });
  }, [chatId, state.convs[chatId]?.messages?.length, state.convs[chatId]?.statusLine]);

  // Keep the highlighted completion row visible as you arrow through the menu.
  useEffect(() => { compItemRef.current?.scrollIntoView({ block: "nearest" }); }, [compIdx]);

  const handleLogin = useCallback(async () => {
    setLoggingIn(true); setLoginLog(""); setAdoptError("");
    // Primary path: the user is already signed into the portal, so mint an API key
    // from that web session and write ~/.ainxt/config.json — no interactive prompt.
    const adopted = await silentAdopt();
    if (adopted.ok) { setAuth({ authenticated: true }); setLoggingIn(false); return; }
    // Show WHY the silent path failed in plain language before falling back.
    setAdoptError(adoptErrorMessage(adopted));
    // Fallback: open the browser for login (device-code / browser prompt via
    // `ainxt login`), then POLL silentAdopt() every 2 s so that once the user
    // completes the web-portal login the session cookie is set and we can mint
    // an API key without waiting for the CLI device-code flow to complete.
    // This fixes the "spinner keeps rotating" issue where the browser opens the
    // portal, the user logs in, but `ainxt login` never detects the completion.
    const off = coworkOnLoginOutput(({ text }) => setLoginLog((s) => (s + text).slice(-4000)));
    const loginPromise = coworkLogin();

    const POLL_INTERVAL_MS = 2000;
    const POLL_DEADLINE = Date.now() + 120_000;
    let pollTimer = null;
    let pollResolved = false;

    const pollAdopt = async () => {
      if (pollResolved || Date.now() > POLL_DEADLINE) return;
      const r = await silentAdopt();
      if (r.ok) {
        pollResolved = true;
        off();
        setAuth({ authenticated: true });
        setLoggingIn(false);
        return;
      }
      pollTimer = setTimeout(pollAdopt, POLL_INTERVAL_MS);
    };
    pollTimer = setTimeout(pollAdopt, POLL_INTERVAL_MS);

    const next = await loginPromise;
    clearTimeout(pollTimer);
    off();

    if (pollResolved) return; // polling already authenticated — don't overwrite

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
      const res = await coworkAdoptToken(key, /* isApiKey */ true);
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

  // ── Clone from Git ────────────────────────────────────────────────────────
  const [showClone, setShowClone] = useState(false);
  const [cloneUrl, setCloneUrl] = useState("");
  const [cloneBranch, setCloneBranch] = useState("");
  const [cloneDest, setCloneDest] = useState("");
  const [cloning, setCloning] = useState(false);
  const [cloneErr, setCloneErr] = useState("");

  const pickCloneDest = useCallback(async () => {
    const d = await pickFolder();
    if (d) setCloneDest(d);
  }, []);

  const doClone = useCallback(async () => {
    setCloneErr("");
    if (!cloneUrl.trim()) { setCloneErr("Enter the repository URL."); return; }
    if (!cloneDest) { setCloneErr("Choose where to clone it."); return; }
    setCloning(true);
    try {
      // Fetch the caller's own GitLab token from their profile (reveal endpoint).
      let token = "";
      const r = await authFetch(`${API_BASE}/profile/tokens/gitlab/value`);
      if (r.status === 404) {
        setCloneErr("No GitLab token in your profile. Add one under Profile → API Token Vault, then retry.");
        setCloning(false); return;
      }
      if (!r.ok) { setCloneErr("Couldn't read your GitLab token from the gateway."); setCloning(false); return; }
      token = (await r.json())?.token || "";
      if (!token) { setCloneErr("Your stored GitLab token is empty."); setCloning(false); return; }

      const res = await coworkClone({ url: cloneUrl.trim(), branch: cloneBranch.trim(), dest: cloneDest, token });
      if (!res?.ok) { setCloneErr(res?.error || "Clone failed."); setCloning(false); return; }

      // Success → open the cloned repo in Code.
      setShowClone(false);
      setCloneUrl(""); setCloneBranch(""); setCloneErr("");
      setFolder(res.path);
    } catch (e) {
      setCloneErr(String(e?.message || e));
    } finally {
      setCloning(false);
    }
  }, [cloneUrl, cloneBranch, cloneDest]);

  const ensureChatSession = useCallback(async () => {
    if (chatId && state.convs[chatId] && state.convs[chatId].status !== "exited") return chatId;
    const res = await coworkCreateSession(folder);
    if (!res?.id) return null;
    dispatch({ type: "ADD", conv: { id: res.id, kind: "chat", cwd: folder, title: "Chat", status: "idle", statusLine: "", messages: [], pendingConfirm: null } });
    // A fresh agent session starts at default model/permission mode — re-apply
    // the user's current choices so they persist across new chats in this tab.
    if (model && model !== MODEL_DEFAULT) coworkSetModel(res.id, model);
    if (permMode && permMode !== "default") coworkSetPermissionMode(res.id, permMode);
    setChatId(res.id);
    return res.id;
  }, [chatId, state.convs, folder, model, permMode]);

  // Pre-warm a session as soon as a folder is chosen, so the SDK initialize
  // handshake runs and the "/" command list is ready BEFORE the first message
  // (the agent only emits the command list after a session boots).
  useEffect(() => {
    if (!isCoworkAvailable || !auth.authenticated) return;
    if (!folder || chatId || openingRef.current) return;
    ensureChatSession();
  }, [folder, chatId, auth.authenticated, ensureChatSession]);

  // Attachments: pick file(s) for the agent to read. The Code agent already has
  // a repo working folder + Read tool, so we keep the files' absolute paths and
  // inject a Read instruction on send (we do NOT repoint the working folder).
  const [attachedFiles, setAttachedFiles] = useState([]);
  // Pick file(s) and add them to the attachment tray, deduped by path. `tagImage`
  // marks images so the chip shows an image glyph and the send prompt hints the
  // (vision-capable) agent to view them.
  const pickAndAttach = useCallback(async (tagImage) => {
    const paths = await pickFile();
    if (!paths || !paths.length) return;
    const items = paths.map((p) => ({
      name: String(p).split(/[\\/]/).pop(), path: p,
      isImage: tagImage && /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(String(p)),
    }));
    setAttachedFiles((prev) => {
      const seen = new Set(prev.map((f) => f.path));
      return [...prev, ...items.filter((f) => !seen.has(f.path))];
    });
  }, []);
  const attachFile = useCallback(() => pickAndAttach(false), [pickAndAttach]);
  const attachImage = useCallback(() => pickAndAttach(true), [pickAndAttach]);
  const clearAttachment = useCallback((path) => {
    setAttachedFiles((prev) => prev.filter((f) => f.path !== path));
  }, []);

  // Speech-to-text via the browser Web Speech API — mirrors Chat.
  const handleMicToggle = useCallback(() => {
    if (isListening) { recognitionRef.current?.stop(); return; }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { window.alert("Voice input isn't supported in this browser."); return; }
    // Detach any prior instance so a rapid re-toggle can't leak orphaned handlers.
    const old = recognitionRef.current;
    if (old) { old.onresult = old.onend = old.onerror = old.onstart = null; try { old.stop(); } catch { /* noop */ } }
    const rec = new SR();
    rec.lang = "en-IN";
    rec.continuous = true;
    rec.interimResults = true;
    rec.onstart = () => setIsListening(true);
    rec.onend = () => setIsListening(false);
    rec.onerror = () => setIsListening(false);
    rec.onresult = (e) => {
      const transcript = Array.from(e.results).map((r) => r[0].transcript).join("");
      setChatInput(transcript);
    };
    recognitionRef.current = rec;
    try { rec.start(); } catch { /* already started */ }
  }, [isListening]);
  // Stop listening if the component unmounts mid-dictation.
  useEffect(() => () => { try { recognitionRef.current?.stop(); } catch { /* noop */ } }, []);

  const sendChat = useCallback(async () => {
    const text = chatInput.trim();
    if ((!text && attachedFiles.length === 0) || !folder) return;

    // Client-handled command: /model [name] switches the live agent's model via
    // the control protocol (the CLI's own /model picker is interactive → no-op
    // headless), so we intercept it here rather than sending it to the agent.
    if (text === "/model" || text.startsWith("/model ")) {
      const arg = text.slice(6).trim();
      const id0 = await ensureChatSession();
      if (!arg) {
        if (id0) dispatch({ type: "EVENT", id: id0, event: { type: "notice", level: "warn", msg: `Current model: ${MODEL_LABEL(model)}. Switch with: ${MODEL_OPTIONS.filter((o) => !o.disabled).map((o) => "/model " + o.value).join("  ·  ")}` } });
        setChatInput("");
        return;
      }
      const target = MODEL_OPTIONS.find((m) => m.value === arg || (m.label || "").toLowerCase() === arg.toLowerCase())?.value
        || MODEL_ALIASES[arg.toLowerCase()]
        || arg;
      setModel(target);
      if (id0) { coworkSetModel(id0, target); dispatch({ type: "EVENT", id: id0, event: { type: "notice", level: "warn", msg: `Model switched to ${MODEL_LABEL(target)}.` } }); }
      setChatInput("");
      return;
    }

    const atts = attachedFiles;
    const baseText = text || "Read the attached file(s) and tell me what they contain.";
    if (!convIdRef.current) { const cid = crypto.randomUUID(); convIdRef.current = cid; setConvId(cid); }
    const id = await ensureChatSession();
    if (!id) return;
    // Capture prior messages BEFORE this turn — for a reopened conversation these
    // are the restored transcript (the fresh agent session has no memory of them).
    const prior = state.convs[id]?.messages || [];
    setChatInput("");
    dispatch({ type: "USER_TURN", id, text: baseText, attachments: atts }); // words + attachment chips
    // First message of a live session (sent to the agent only — not shown):
    //  - working-directory context so it reads the repo itself (--bare strips it);
    //  - if this is a REOPENED conversation, replay the prior exchange so the
    //    agent has continuity instead of re-exploring from scratch.
    let toSend = baseText;
    // Slash commands (e.g. /cost, /compact, /review) must reach the agent
    // verbatim — never wrap them in the working-dir preamble (that would stop
    // them being parsed as commands). Don't mark primed, so the next real
    // message still gets the context preamble.
    const isSlash = baseText.startsWith("/");
    if (!isSlash && !primedRef.current.has(id)) {
      primedRef.current.add(id);
      let pre = `[Working directory: ${folder} — a code repository. Read and explore the project's files yourself using your tools (Bash, Read, Glob, Grep); NEVER ask me to paste, share, or upload code — it is already on disk here.]`;
      const transcript = prior
        .map((m) => {
          const t = (m.blocks || []).filter((b) => b.kind === "text").map((b) => b.text).join(" ").trim();
          return t ? `${m.role === "user" ? "User" : "Assistant"}: ${t}` : "";
        })
        .filter(Boolean)
        .join("\n\n");
      if (transcript) pre += `\n\n[This conversation continues from earlier — you already did the work below; don't redo it. Prior exchange:\n${transcript}\n]`;
      toSend = `${pre}\n\nMy message:\n${baseText}`;
    }
    // Attached files: tell the agent to read them (absolute paths) and use them.
    if (atts.length) {
      const imgs = atts.filter((f) => f.isImage);
      const docs = atts.filter((f) => !f.isImage);
      const parts = [];
      if (docs.length) parts.push(`file(s): ${docs.map((f) => f.path).join(", ")}`);
      if (imgs.length) parts.push(`image(s): ${imgs.map((f) => f.path).join(", ")}`);
      toSend = `[The user attached ${parts.join(" and ")} for this task. Open each with your Read tool (it can view images) and use their contents.]\n\n${toSend}`;
      setAttachedFiles([]);
    }
    coworkRun(id, toSend, model, false);
  }, [chatInput, folder, attachedFiles, ensureChatSession, model, state.convs]);

  // Regenerate the assistant reply at `assistantIdx` (any message, not just the
  // last): find the user message that prompted it, drop that reply + everything
  // after it, then re-ask the same user turn as a fresh turn.
  const regenerate = useCallback(async (assistantIdx) => {
    const cid = chatIdRef.current;
    const conv = cid ? convsRef.current[cid] : null;
    if (!conv || conv.status === "running") return;
    const msgs = conv.messages;
    // Resolve target: explicit index, else the last assistant message.
    let idx = typeof assistantIdx === "number" ? assistantIdx : -1;
    if (idx < 0 || idx >= msgs.length || msgs[idx]?.role !== "assistant") {
      idx = msgs.map((m, i) => (m.role === "assistant" ? i : -1)).filter((i) => i >= 0).at(-1) ?? -1;
    }
    if (idx < 0) return;
    // Walk back to the user message that prompted this reply.
    let userIdx = -1;
    for (let i = idx - 1; i >= 0; i--) { if (msgs[i].role === "user") { userIdx = i; break; } }
    if (userIdx < 0) return;
    const userMsg = msgs[userIdx];
    const text = userMsg?.blocks?.[0]?.text;
    if (!text) return;
    // Truncate to before the prompting user turn, then re-ask (USER_TURN
    // re-appends the user message + a fresh streaming assistant slot).
    dispatch({ type: "TRUNCATE_TO", id: cid, count: userIdx });
    dispatch({ type: "USER_TURN", id: cid, text, attachments: userMsg.attachments || [] });
    coworkRun(cid, text, model, false);
  }, [model]);

  // Answer a permission prompt: send the response to the CLI (which resumes
  // streaming) and optimistically clear the dialog via a synthetic event.
  const onPermissionAnswer = useCallback((sessionId, confirmId, answer) => {
    const conv = state.convs[sessionId];
    if (!conv?.pendingConfirm) return;
    coworkRespondConfirm(sessionId, confirmId, answer);
    // optimistically clear the dialog; subsequent events resume streaming
    dispatch({ type: "EVENT", id: sessionId, event: { type: "__clear_confirm" } });
  }, [state.convs]);

  // ── Render gates ──────────────────────────────────────────────────────────
  if (!isCoworkAvailable) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <div className="max-w-md text-center">
          <MonitorSmartphone className="w-10 h-10 text-indigo-500 mx-auto mb-3" />
          <h2 className="text-lg font-semibold text-gray-800 mb-1">Buddy runs in the desktop app</h2>
          <p className="text-sm text-gray-500">Local-agent mode works on your machine's files and terminal, which a browser can't access. Open AiNxt Desktop to use Buddy.</p>
        </div>
      </div>
    );
  }

  if (!auth.authenticated) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <div className="max-w-lg w-full">
          <div className="text-center mb-4">
            <Cpu className="w-10 h-10 text-indigo-500 mx-auto mb-3" />
            <h2 className="text-lg font-semibold text-gray-800 mb-1">Enable local-agent mode</h2>
            <p className="text-sm text-gray-500">Sign in to the AiNxt CLI once to let the agent work on your local repositories.</p>
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

  const chat = chatId ? state.convs[chatId] : null;
  const chatBusy = chat?.status === "running";
  // A pending edit-permission tied to a specific file → lets the editor panel
  // surface Accept/Reject for it (same decision as the chat's permission bar).
  const pendingRel = (() => {
    const pc = chat?.pendingConfirm;
    if (!pc || !folder || !/^(Edit|Write|MultiEdit)$/.test(pc.tool || "")) return null;
    return normalizeRel(pc.detail || "", folder);
  })();
  const answerActiveConfirm = (ans) => {
    if (chat?.pendingConfirm) onPermissionAnswer(chatId, chat.pendingConfirm.id, ans);
  };

  // Model + permission-mode controls (local state is the source of truth; pushed
  // to the live session and re-applied to any new session via ensureChatSession).
  const onModelChange = (value) => { setModel(value); if (chatIdRef.current) coworkSetModel(chatIdRef.current, value); };
  const onPermModeChange = (value) => { setPermMode(value); if (chatIdRef.current) coworkSetPermissionMode(chatIdRef.current, value); };

  // Unified completion menu: "/" slash-commands (when the line starts with "/")
  // or "@" file-mentions (when the active word starts with "@"). Each item has
  // {label, hint, desc, insert} where insert is the FULL new input after accept.
  const slashCmds = chat?.slashCommands || [];
  const allCmds = [{ name: "model", description: "switch model", argumentHint: "[name]" }, ...slashCmds];
  const slashTyping = chatInput.startsWith("/") && !chatInput.includes(" ") && !chatInput.includes("\n");
  const atMatch = chatInput.match(/(^|\s)@([^\s@]*)$/);   // active @token at end of input
  let compItems = [];
  if (slashTyping) {
    const q = chatInput.slice(1).toLowerCase();
    compItems = allCmds.filter((c) => c.name.toLowerCase().startsWith(q)).slice(0, 50).map((c) => ({
      label: `/${c.name}`, hint: c.argumentHint, desc: c.description, insert: `/${c.name} `,
    }));
  } else if (atMatch) {
    const q = atMatch[2].toLowerCase();
    const base = chatInput.slice(0, atMatch.index + atMatch[1].length); // text before the "@"
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
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  };

  // Context-window usage for the footer bar.
  const ctxPct = typeof chat?.contextPct === "number" ? Math.round(chat.contextPct) : null;
  const sessionCost = chat?.costTotal;

  // Sidebar list = the live conversation (shown immediately, even before it's
  // saved) + the persisted ones (deduped). So it's never empty while chatting.
  const _folderName = (folder || "").split("/").filter(Boolean).pop() || "";
  const _activeTitle = (chat?.messages?.find((m) => m.role === "user")?.blocks?.[0]?.text || "").slice(0, 60);
  // Stable list; the active conversation stays in its place (just highlighted).
  // Only a brand-new conversation (not yet persisted) is shown — at the top.
  const displayConvs = [...conversations];
  if (convId && _activeTitle && !displayConvs.some((c) => c.id === convId)) {
    displayConvs.unshift({ id: convId, folder, folderName: _folderName, title: _activeTitle });
  }

  return (
    <div className={`h-full flex flex-col bg-white relative ${dark ? "code-dark" : ""}`}>
      {/* Top bar */}
      <div className="flex items-center gap-3 px-6 py-3 bg-white border-b border-gray-200 shrink-0">
        <BrandMark className="w-6 h-6" />
        <div className="flex flex-col min-w-0">
          <div className="flex items-center gap-1.5">
            <h1 className="text-base font-semibold text-gray-900 leading-tight">Code</h1>
            {convId && (() => {
              const activeConv = conversations.find((c) => c.id === convId);
              if (!activeConv) return null;
              return editingTitle === convId ? (
                <form onSubmit={(e) => { e.preventDefault(); const t = editTitle.trim(); if (t) { convRename(convId, t); setConversations((prev) => prev.map((x) => x.id === convId ? { ...x, title: t } : x)); } setEditingTitle(null); }}
                  className="inline-flex items-center ml-2">
                  <input autoFocus value={editTitle} onChange={(e) => setEditTitle(e.target.value)}
                    className="text-sm border border-indigo-300 rounded px-1.5 py-0.5 outline-none font-medium text-gray-700 w-48"
                    onKeyDown={(e) => e.key === "Escape" && setEditingTitle(null)}
                    onBlur={() => setEditingTitle(null)} />
                </form>
              ) : (
                <button onClick={() => { setEditTitle(activeConv.title); setEditingTitle(convId); }}
                  className="ml-2 flex items-center gap-1 text-sm text-gray-500 hover:text-indigo-600 truncate max-w-[20rem] group/edit">
                  <span className="truncate">{activeConv.title}</span>
                  <Pencil className="w-3 h-3 shrink-0 opacity-0 group-hover/edit:opacity-100 transition" />
                </button>
              );
            })()}
          </div>
          <p className="text-xs text-gray-500 leading-tight">Local coding agent — runs on your machine</p>
        </div>
        <button onClick={() => { setCloneErr(""); setShowClone(true); }}
          className="ml-auto flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-700"
          title="Clone a repository from a Git URL using your profile token">
          <GitBranch className="w-4 h-4 text-indigo-600" /> Clone from Git
        </button>
        <button onClick={chooseFolder}
          className="flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-700">
          <FolderOpen className="w-4 h-4 text-indigo-600" />
          {folder ? <span className="font-mono text-xs truncate max-w-[18rem]">{folder}</span> : "Choose working folder"}
        </button>
        {folder && (
          <div className="flex items-center rounded-lg border border-gray-200 overflow-hidden text-xs" title="Where the file editor opens">
            <button onClick={() => setLayoutMode("tabbed")}
              className={`flex items-center gap-1 px-2 py-1.5 ${layoutMode === "tabbed" ? "bg-indigo-50 text-indigo-700" : "text-gray-600 hover:bg-gray-50"}`}
              title="Tabbed — editor replaces the chat area">
              <SquareStack className="w-3.5 h-3.5" /> Tabbed
            </button>
            <button onClick={() => setLayoutMode("split")}
              className={`flex items-center gap-1 px-2 py-1.5 border-l border-gray-200 ${layoutMode === "split" ? "bg-indigo-50 text-indigo-700" : "text-gray-600 hover:bg-gray-50"}`}
              title="Split — editor beside the chat">
              <Columns2 className="w-3.5 h-3.5" /> Split
            </button>
          </div>
        )}
        <button onClick={() => setDark((d) => !d)}
          className={`flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-700 ${folder ? "" : "ml-auto"}`}
          title={dark ? "Switch to light theme" : "Switch to dark high-contrast (IDE) theme"}>
          {dark ? <Sun className="w-4 h-4 text-amber-500" /> : <Moon className="w-4 h-4 text-indigo-600" />}
        </button>
      </div>

      {/* Clone-from-Git modal */}
      {showClone && (
        <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/30" onMouseDown={() => !cloning && setShowClone(false)}>
          <div className="bg-white rounded-xl shadow-xl border border-gray-200 w-[34rem] p-5" onMouseDown={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 mb-3">
              <GitBranch className="w-5 h-5 text-indigo-600" />
              <h3 className="font-semibold text-gray-800">Clone a repository</h3>
              <button onClick={() => !cloning && setShowClone(false)} className="ml-auto p-1 rounded text-gray-400 hover:text-gray-700"><X className="w-4 h-4" /></button>
            </div>
            <p className="text-xs text-gray-500 mb-3">Uses the GitLab token from your <span className="font-medium">Profile → API Token Vault</span>. You can only clone repos that token can access.</p>

            <label className="block text-xs font-medium text-gray-600 mb-1">Repository URL (https)</label>
            <input value={cloneUrl} onChange={(e) => setCloneUrl(e.target.value)} disabled={cloning}
              placeholder="https://gitlab.example.com/group/repo.git"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400 mb-3 font-mono" />

            <label className="block text-xs font-medium text-gray-600 mb-1">Branch <span className="text-gray-400 font-normal">(optional — default branch if blank)</span></label>
            <input value={cloneBranch} onChange={(e) => setCloneBranch(e.target.value)} disabled={cloning}
              placeholder="main"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400 mb-3 font-mono" />

            <label className="block text-xs font-medium text-gray-600 mb-1">Clone into</label>
            <div className="flex items-center gap-2 mb-1">
              <button onClick={pickCloneDest} disabled={cloning}
                className="flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-700 shrink-0">
                <FolderOpen className="w-4 h-4 text-indigo-600" /> Choose location
              </button>
              <span className="text-xs font-mono text-gray-500 truncate">{cloneDest || "no folder chosen"}</span>
            </div>
            {cloneDest && cloneUrl.trim() && (
              <p className="text-[11px] text-gray-400 mb-2 font-mono truncate">→ {cloneDest}/{(cloneUrl.trim().replace(/\/+$/, "").replace(/\.git$/i, "").split("/").pop() || "repo")}</p>
            )}

            {cloneErr && <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-md px-2 py-1.5 my-2">{cloneErr}</div>}

            <div className="flex justify-end gap-2 mt-4">
              <button onClick={() => !cloning && setShowClone(false)} disabled={cloning}
                className="px-3 py-1.5 text-sm rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50">Cancel</button>
              <button onClick={doClone} disabled={cloning || !cloneUrl.trim() || !cloneDest}
                className="px-3 py-1.5 text-sm rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40 flex items-center gap-1.5">
                {cloning ? <><Loader2 className="w-4 h-4 animate-spin" /> Cloning…</> : <>Clone &amp; open</>}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Body: explorer/history rail · chat column · editor panel */}
      <div className="flex-1 flex min-h-0">
        {/* Left rail — Files (project explorer) / Chats (conversation history) */}
        <div className="w-60 border-r border-gray-200 bg-gray-50 flex flex-col shrink-0 min-h-0">
          <div className="flex items-center gap-1 p-2 pb-1.5">
            <button onClick={() => setRailTab("files")}
              className={`flex-1 flex items-center justify-center gap-1.5 text-xs rounded-md py-1.5 ${railTab === "files" ? "bg-white border border-gray-200 text-gray-800 shadow-sm" : "text-gray-500 hover:bg-gray-100"}`}>
              <FolderTree className="w-3.5 h-3.5" /> Files
            </button>
            <button onClick={() => setRailTab("chats")}
              className={`flex-1 flex items-center justify-center gap-1.5 text-xs rounded-md py-1.5 ${railTab === "chats" ? "bg-white border border-gray-200 text-gray-800 shadow-sm" : "text-gray-500 hover:bg-gray-100"}`}>
              <MessageSquare className="w-3.5 h-3.5" /> Chats
            </button>
          </div>

          {railTab === "files" ? (
            folder ? (
              <>
                {refreshErr && (
                  <div className="mx-2 mb-1 text-[11px] text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1">
                    Refresh failed: {refreshErr}
                  </div>
                )}
                <FileExplorer files={files} changed={changedSet} activeFile={activeFile}
                  onOpen={(rel) => openFile(rel, "edit")}
                  onCreate={onCreateFile} onRename={onRenameFile} onDelete={onDeleteFile}
                  onRefresh={refreshFiles} refreshing={refreshing} />
              </>
            ) : (
              <p className="text-xs text-gray-400 px-3 mt-2">Choose a folder to see its files.</p>
            )
          ) : (
            <>
              <div className="px-2 pb-2">
                <button onClick={newChat}
                  className="w-full flex items-center justify-center gap-1.5 text-sm bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg py-1.5">
                  <Plus className="w-4 h-4" /> New session
                </button>
              </div>
              <div className="px-3 pb-1 text-[11px] uppercase tracking-wide text-gray-400">Conversations</div>
              <div className="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5">
                {displayConvs.length === 0 && (
                  <p className="text-xs text-gray-400 px-2 mt-2">{folder ? "Send a message — your conversations appear here." : "Choose a folder to start a project."}</p>
                )}
                {displayConvs.map((c) => (
                  <div key={c.id} className={`group relative rounded-md ${convId === c.id ? "bg-indigo-100" : "hover:bg-gray-100"}`}>
                    {editingTitle === c.id ? (
                      <form onSubmit={(e) => { e.preventDefault(); const t = editTitle.trim(); if (t) { convRename(c.id, t); setConversations((prev) => prev.map((x) => x.id === c.id ? { ...x, title: t } : x)); } setEditingTitle(null); }}
                        onBlur={() => setEditingTitle(null)}
                        className="flex items-center gap-1 px-2 py-1"
                        onClick={(e) => e.stopPropagation()}>
                        <input autoFocus value={editTitle} onChange={(e) => setEditTitle(e.target.value)}
                          className="flex-1 text-sm border border-indigo-300 rounded px-1.5 py-0.5 outline-none bg-white font-medium text-gray-800"
                          onKeyDown={(e) => e.key === "Escape" && setEditingTitle(null)} />
                      </form>
                    ) : (
                      <>
                        <button onClick={() => openConversation(c)} title={`${c.folderName} — ${c.title}`}
                          className="w-full flex items-start gap-2 text-left px-2 py-1.5 pr-7">
                          <MessageSquare className="w-3.5 h-3.5 shrink-0 mt-0.5 text-gray-400" />
                          <span className="min-w-0 flex-1">
                            <span className={`block text-sm truncate ${convId === c.id ? "text-indigo-800 font-medium" : "text-gray-800"}`}>{c.title}</span>
                            <span className="block text-[11px] text-gray-400 truncate">{c.folderName || "Project"}</span>
                          </span>
                        </button>
                        <button onClick={(e) => { e.stopPropagation(); setEditTitle(c.title); setEditingTitle(c.id); }} title="Rename conversation"
                          className="absolute right-7 top-1.5 p-1 rounded text-gray-400 opacity-0 group-hover:opacity-100 hover:text-indigo-600 hover:bg-white/60 transition">
                          <Pencil className="w-3.5 h-3.5" />
                        </button>
                        <button onClick={(e) => deleteConversation(c, e)} title="Delete conversation"
                          className="absolute right-1 top-1.5 p-1 rounded text-gray-400 opacity-0 group-hover:opacity-100 hover:text-red-600 hover:bg-white/60 transition">
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        {/* Center column — chat, and (tabbed mode) the file editor under a tab bar */}
        <div className="flex-1 flex flex-col min-h-0 min-w-0 relative">
        {layoutMode === "tabbed" && openFiles.length > 0 && (
          <div className="flex items-stretch border-b border-gray-200 bg-gray-50 shrink-0 overflow-x-auto">
            <button onClick={() => setCenterTab("chat")}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-xs border-r border-gray-200 ${centerTab === "chat" ? "bg-white text-gray-800" : "text-gray-500 hover:bg-gray-100"}`}>
              <MessageSquare className="w-3.5 h-3.5" /> Chat
            </button>
            {openFiles.map((rel) => (
              <div key={rel} onClick={() => { setActiveFile(rel); setCenterTab(rel); }} title={rel}
                className={`group flex items-center gap-1.5 px-3 py-1.5 text-xs border-r border-gray-200 cursor-pointer max-w-[12rem] ${centerTab === rel ? "bg-white text-gray-800" : "text-gray-500 hover:bg-gray-100"}`}>
                <FileText className={`w-3.5 h-3.5 shrink-0 ${changedSet.has(rel) ? "text-emerald-600" : "text-gray-400"}`} />
                <span className="truncate">{rel.split("/").pop()}</span>
                <button onClick={(e) => { e.stopPropagation(); closeTab(rel); }}
                  className="p-0.5 rounded text-gray-400 opacity-0 group-hover:opacity-100 hover:text-red-600 shrink-0"><X className="w-3 h-3" /></button>
              </div>
            ))}
          </div>
        )}
        {/* Chat view — kept mounted (preserves stream/scroll); hidden when a file tab is active */}
        <div className={layoutMode === "tabbed" && centerTab !== "chat" ? "hidden" : "flex-1 flex flex-col min-h-0"}>
        <div ref={chatScrollRef} className="flex-1 overflow-y-auto overflow-x-hidden px-6 py-8 leading-5">
          {!chat || chat.messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full gap-4 text-center">
              <div className="w-16 h-16 rounded-full brand-grad-vivid flex items-center justify-center shadow-lg">
                <BrandMark className="w-8 h-8" alt="AiNxt Code" />
              </div>
              <div>
                <p className="text-lg font-semibold text-gray-800">{folder ? "Ask about or change this repo" : "Open a folder to begin"}</p>
                <p className="text-sm text-gray-400 mt-1 max-w-sm">{folder
                  ? "The agent runs on your machine — it can read, edit, and run commands in this folder."
                  : "Choose a local folder, or clone a repo from Git using your profile token."}</p>
              </div>
              {!folder && (
                <div className="flex items-center gap-2">
                  <button onClick={chooseFolder}
                    className="flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-700">
                    <FolderOpen className="w-4 h-4 text-indigo-600" /> Open folder
                  </button>
                  <button onClick={() => { setCloneErr(""); setShowClone(true); }}
                    className="flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white">
                    <GitBranch className="w-4 h-4" /> Clone from Git
                  </button>
                </div>
              )}
            </div>
          ) : (
            <>
              {chat.messages.map((m, i) => (
                <MessageRow key={i} m={m} index={i}
                  busy={chatBusy} onRegenerate={regenerate}
                  statusLine={m.streaming ? chat?.statusLine : null} />
              ))}
              {/* Fallback status line: a live backend status exists but the
                  streaming assistant message hasn't mounted yet. */}
              {chat?.statusLine && !chat.messages.some((m) => m.streaming) && (
                <div className="mb-6"><AiNxtSpinner label={chat.statusLine} /></div>
              )}
            </>
          )}
        </div>
        <div className="border-t border-gray-100 bg-white px-4 pb-3 pt-3 shrink-0">
          <PermissionBar conv={chat} onAnswer={onPermissionAnswer} />
          <div className="relative">
            {/* Completion menu — "/" commands or "@" files. ↑/↓ to move, ⏎/Tab to accept, Esc to close. */}
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
            <div className={`border rounded-xl bg-gray-50 transition-colors ${chatBusy ? "border-gray-200" : "border-gray-300 focus-within:border-gray-400 focus-within:bg-white"}`}>
              {/* Attached-file chips */}
              {attachedFiles.length > 0 && (
                <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
                  {attachedFiles.map((f) => (
                    <span key={f.path} title={f.path}
                      className="inline-flex items-center gap-1 max-w-[16rem] text-xs bg-indigo-50 text-indigo-700 border border-indigo-100 rounded-md px-1.5 py-0.5">
                      {f.isImage ? <ImageIcon size={12} className="shrink-0" /> : <Paperclip size={12} className="shrink-0" />}
                      <span className="truncate">{f.name}</span>
                      <button onClick={() => clearAttachment(f.path)} className="ml-0.5 text-indigo-400 hover:text-indigo-700 cursor-pointer"><X size={12} /></button>
                    </span>
                  ))}
                </div>
              )}
              <textarea ref={textareaRef} value={chatInput} disabled={!folder}
                onChange={(e) => { setChatInput(e.target.value); setCompIdx(0); setCompDismissed(false); }}
                onKeyDown={onInputKeyDown}
                placeholder={folder ? "Message the local agent…  (/ commands · @ files)" : "Choose a working folder first"}
                rows={3}
                className="w-full resize-none bg-transparent px-3 pt-3 pb-1 outline-none text-sm text-gray-800 placeholder-gray-400 disabled:opacity-60" />
              <div className="flex items-center gap-1 px-2 pb-2">
                <button onClick={attachFile} disabled={!folder} title="Attach file(s) for the agent to read"
                  className="p-1.5 cursor-pointer text-gray-500 hover:text-gray-800 transition disabled:opacity-30">
                  <Paperclip size={18} />
                </button>
                <button onClick={attachImage} disabled={!folder} title="Attach image(s) — the agent reads them from disk"
                  className="p-1.5 cursor-pointer text-gray-500 hover:text-gray-800 transition disabled:opacity-30">
                  <ImageIcon size={18} />
                </button>
                <button onClick={handleMicToggle} disabled={!folder} title={isListening ? "Stop dictation" : "Dictate with your voice"}
                  className={`p-1.5 cursor-pointer transition disabled:opacity-30 ${isListening ? "text-red-500 animate-pulse" : "text-gray-500 hover:text-gray-800"}`}>
                  {isListening ? <MicOff size={18} /> : <Mic size={18} />}
                </button>
                <div className="flex-1" />
                <button onClick={chatBusy ? () => coworkInterrupt(chatId) : sendChat} disabled={!chatBusy && (!folder || (!chatInput.trim() && attachedFiles.length === 0))}
                  className="p-1.5 cursor-pointer text-gray-500 hover:text-gray-800 transition disabled:opacity-30">
                  {chatBusy ? <CirclePauseIcon size={20} /> : <SendHorizontal size={20} />}
                </button>
              </div>
            </div>
          </div>
          {/* Status bar: model · permission mode · context · session cost */}
          <div className="flex items-center gap-3 mt-1.5 px-1 text-[11px] text-gray-400">
            <label className="flex items-center gap-1 cursor-pointer hover:text-gray-600" title="Model (or type /model)">
              <Cpu className="w-3 h-3" />
              <select value={model} onChange={(e) => onModelChange(e.target.value)} onFocus={refreshModelLists}
                className="bg-transparent outline-none cursor-pointer text-gray-500 hover:text-gray-700 max-w-[220px]">
                {MODEL_OPTIONS.map((o) => {
                  const skip = o.disabled;
                  const badge = skip ? null : _modelContextBadge(o.value, o.label);
                  const tier = skip ? null : _modelTierTag(o.tier);
                  const suffix = [badge, tier].filter(Boolean).join(" · ");
                  return (
                    <option key={o.value} value={o.value} disabled={o.disabled}>
                      {suffix ? `${o.label} · ${suffix}` : o.label}
                    </option>
                  );
                })}
              </select>
            </label>
            <span className="text-gray-200">|</span>
            <label className="flex items-center gap-1 cursor-pointer hover:text-gray-600" title="Permission mode">
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
        {/* Tabbed mode: the editor replaces the chat area when a file tab is active */}
        {layoutMode === "tabbed" && centerTab !== "chat" && folder && (
          <div className="flex-1 min-h-0 min-w-0">
            <FileEditorPanel
              showTabs={false} dark={dark}
              openFiles={openFiles} activeFile={centerTab} mode={viewerMode}
              changedFiles={changedFiles} absOf={absOf}
              onCloseTab={closeTab} onSetMode={setViewerMode} onSaved={onSaved}
              reloadSignal={reloadSignal} reloadTarget={reloadTarget}
              pendingConfirm={chat?.pendingConfirm} pendingRel={pendingRel} onAnswer={answerActiveConfirm} />
          </div>
        )}
        </div>

        {/* Split mode: the editor sits beside the chat (resizable) */}
        {layoutMode === "split" && panelOpen && folder && (
          <>
            <div onMouseDown={startResize}
              className="w-1 cursor-col-resize bg-gray-200 hover:bg-indigo-400 transition-colors shrink-0" />
            <div style={{ width: panelWidth }} className="shrink-0 min-w-0 border-l border-gray-200 bg-white">
              <FileEditorPanel
                dark={dark}
                openFiles={openFiles} activeFile={activeFile} mode={viewerMode}
                changedFiles={changedFiles} absOf={absOf}
                onSelectTab={(rel) => { setActiveFile(rel); setViewerMode("edit"); }}
                onCloseTab={closeTab} onSetMode={setViewerMode}
                onClose={() => setPanelOpen(false)} onSaved={onSaved}
                reloadSignal={reloadSignal} reloadTarget={reloadTarget}
                pendingConfirm={chat?.pendingConfirm} pendingRel={pendingRel} onAnswer={answerActiveConfirm} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

