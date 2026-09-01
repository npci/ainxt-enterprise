// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useRef, useCallback } from "react";
import { Folder, FolderOpen, Eye, EyeOff, Trash2, RefreshCw, FileText,
         Clipboard, Zap, AlertCircle, CheckCircle } from "lucide-react";
import {
  isDesktop, pickFolder, listFolder, watchFolder, unwatchFolder,
  getWatchedFolders, onWorkspaceChange, offWorkspaceChange,
  onClipboardChange, offClipboardChange, getClipboard,
  onShortcutContext, offShortcutContext,
  getMcpPort, onMcpServerReady,
} from "../hooks/useDesktop.js";

import { authFetch } from "../config.js";

const API = "/ainxt/v1/api";

// ── Clipboard popup ──────────────────────────────────────────────────────────

function ClipboardPopup({ text, onAsk, onDismiss }) {
  if (!text) return null;
  const preview = text.length > 120 ? text.slice(0, 120) + "…" : text;
  const looksLikeError = /error:|exception|failed|traceback/i.test(text);
  const looksLikeCode  = text.trim().startsWith("{") || text.includes("def ") || text.includes("function ");

  return (
    <div className="fixed bottom-4 right-4 z-50 w-80 bg-white border border-indigo-200 rounded-xl shadow-xl p-3 text-xs">
      <div className="flex items-center gap-2 mb-2">
        <Clipboard className="w-3.5 h-3.5 text-indigo-600 shrink-0" />
        <span className="text-indigo-700 font-medium">Clipboard captured</span>
        <button onClick={onDismiss} className="ml-auto text-gray-400 hover:text-gray-700 text-xs">✕</button>
      </div>
      <p className="text-gray-600 font-mono break-all mb-2">{preview}</p>
      <div className="flex gap-2">
        {looksLikeError && (
          <button onClick={() => onAsk(`Fix this error:\n${text}`)}
            className="flex-1 bg-red-600 hover:bg-red-700 text-white rounded px-2 py-1 text-xs transition">
            Fix error
          </button>
        )}
        {looksLikeCode && (
          <button onClick={() => onAsk(`Explain this code:\n${text}`)}
            className="flex-1 bg-indigo-600 hover:bg-indigo-700 text-white rounded px-2 py-1 text-xs transition">
            Explain
          </button>
        )}
        <button onClick={() => onAsk(text)}
          className="flex-1 bg-indigo-600 hover:bg-indigo-700 text-white rounded px-2 py-1 text-xs transition">
          Ask AiNxt
        </button>
      </div>
    </div>
  );
}

// ── Workspace row ────────────────────────────────────────────────────────────

function WorkspaceRow({ folder, onRemove, onAsk }) {
  const [status, setStatus]     = useState(null);  // {chunk_count, last_indexed}
  const [indexing, setIndexing] = useState(false);
  const [watching, setWatching] = useState(true);
  const [error, setError]       = useState("");

  const workspaceName = folder.split("/").pop() || folder;

  useEffect(() => { loadStatus(); }, [folder]);

  async function loadStatus() {
    try {
      const res = await authFetch(API + `/desktop/index/${encodeURIComponent(workspaceName)}/status`);
      if (res.ok) setStatus(await res.json());
    } catch { /* non-critical */ }
  }

  async function reindex() {
    setIndexing(true); setError("");
    try {
      const files = await listFolder(folder, { maxFiles: 500 });
      if (!files?.length) { setError("No supported files found"); setIndexing(false); return; }

      // Batch read all files
      const { readFile } = await import("../hooks/useDesktop.js");
      const filePayloads = [];
      for (const f of files) {
        const { content } = await readFile(f.path);
        if (content) filePayloads.push({ filename: f.path, content });
      }

      const res = await authFetch(API + "/desktop/index/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace: workspaceName, files: filePayloads }),
      });
      if (!res.ok) throw new Error(`Index failed: ${res.status}`);
      const { job_id } = await res.json();
      setError(""); loadStatus();
    } catch (e) {
      setError(e.message);
    } finally {
      setIndexing(false);
    }
  }

  async function toggleWatch() {
    if (watching) {
      await unwatchFolder(folder);
      setWatching(false);
    } else {
      const r = await watchFolder(folder);
      setWatching(r?.watching ?? false);
    }
  }

  async function remove() {
    await unwatchFolder(folder);
    await authFetch(API + `/desktop/index/${encodeURIComponent(workspaceName)}`, { method: "DELETE" });
    onRemove(folder);
  }

  return (
    <div className="bg-white border border-gray-200 hover:border-indigo-300 hover:shadow-sm rounded-lg p-3 transition">
      <div className="flex items-center gap-2 mb-2">
        <FolderOpen className="w-4 h-4 text-yellow-500 shrink-0" />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-gray-900 truncate">{workspaceName}</p>
          <p className="text-xs text-gray-500 truncate">{folder}</p>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button onClick={toggleWatch} title={watching ? "Stop watching" : "Start watching"}
            className={`p-1 rounded transition ${watching ? "text-green-600 hover:text-green-700" : "text-gray-400 hover:text-gray-600"}`}>
            {watching ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
          </button>
          <button onClick={reindex} disabled={indexing} title="Re-index folder"
            className="p-1 rounded text-indigo-600 hover:text-indigo-700 disabled:opacity-40 transition">
            <RefreshCw className={`w-3.5 h-3.5 ${indexing ? "animate-spin" : ""}`} />
          </button>
          <button onClick={() => onAsk(`Ask about workspace: ${workspaceName}`)} title="Ask about this workspace"
            className="p-1 rounded text-purple-600 hover:text-purple-700 transition">
            <Zap className="w-3.5 h-3.5" />
          </button>
          <button onClick={remove} title="Remove workspace"
            className="p-1 rounded text-red-500 hover:text-red-600 transition">
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      <div className="flex items-center gap-3 text-xs">
        {status ? (
          <>
            <span className="text-gray-600">{status.chunk_count?.toLocaleString() || 0} chunks</span>
            {status.last_indexed && (
              <span className="text-gray-400">
                last indexed {new Date(status.last_indexed).toLocaleDateString()}
              </span>
            )}
          </>
        ) : (
          <span className="text-gray-400">not indexed yet</span>
        )}
        {watching && <span className="text-green-600 ml-auto">● live</span>}
      </div>

      {error && <p className="text-xs text-red-600 mt-1">{error}</p>}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function LocalFiles({ onAskWithContext }) {
  const [workspaces, setWorkspaces]     = useState([]);
  const [clipPopup, setClipPopup]       = useState(null);
  const [mcpStatus, setMcpStatus]       = useState(null); // null|"registering"|"ok"|"error"
  const [mcpError, setMcpError]         = useState("");
  const [recentChange, setRecentChange] = useState(null);
  const pendingReindex = useRef(new Set());

  // Load persisted watched folders on mount
  useEffect(() => {
    if (!isDesktop) return;
    getWatchedFolders().then(folders => {
      if (folders?.length) setWorkspaces(folders);
    });
  }, []);

  // Phase 2: workspace file change listener → auto re-index changed file
  useEffect(() => {
    if (!isDesktop) return;
    const handler = async ({ filename, folder }) => {
      setRecentChange(filename.split("/").pop() || filename);
      setTimeout(() => setRecentChange(null), 3000);

      // Debounce: avoid re-indexing the same file more than once per 2s
      if (pendingReindex.current.has(filename)) return;
      pendingReindex.current.add(filename);
      setTimeout(() => pendingReindex.current.delete(filename), 2000);

      try {
        const { readFile } = await import("../hooks/useDesktop.js");
        const { content } = await readFile(filename);
        if (!content) return;
        const workspaceName = folder.split("/").pop() || folder;
        await authFetch(API + "/desktop/index/file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workspace: workspaceName, filename, content }),
        });
      } catch { /* non-critical */ }
    };

    onWorkspaceChange(handler);
    return () => offWorkspaceChange(handler);
  }, []);

  // Phase 3: clipboard change listener
  useEffect(() => {
    if (!isDesktop) return;
    const handler = ({ text }) => setClipPopup(text);
    onClipboardChange(handler);
    return () => offClipboardChange(handler);
  }, []);

  // Phase 4: shortcut context — pre-fill chat input when summoned via Cmd+Shift+A
  useEffect(() => {
    if (!isDesktop) return;
    const handler = ({ clipboard: clip, activeApp }) => {
      if (clip && clip.trim().length > 10 && onAskWithContext) {
        // Surface as a suggestion rather than sending immediately
        setClipPopup(clip);
      }
    };
    onShortcutContext(handler);
    return () => offShortcutContext(handler);
  }, [onAskWithContext]);

  // Phase 5: MCP server ready → register with backend using cookie auth (renderer-side)
  useEffect(() => {
    if (!isDesktop) return;
    const handler = async () => {
      setMcpStatus("registering");
      try {
        const port = await getMcpPort();
        if (!port) throw new Error("MCP server not started");
        // Fetch tool list directly from local MCP server
        const toolsRes = await fetch(`http://127.0.0.1:${port}/tools`);
        const { tools } = toolsRes.ok ? await toolsRes.json() : { tools: [] };
        // Register with backend using cookie auth (credentials: 'include')
        const res = await authFetch(API + "/desktop/register-mcp", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ port, tools }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        setMcpStatus("ok");
      } catch (e) {
        setMcpStatus("error");
        setMcpError(e.message || "Registration failed");
      }
    };
    onMcpServerReady(handler);
    // Try to register immediately in case MCP server already started
    handler();
  }, []);

  async function addWorkspace() {
    const folder = await pickFolder();
    if (!folder) return;
    if (workspaces.includes(folder)) return;
    await watchFolder(folder);
    setWorkspaces(prev => [...prev, folder]);
  }

  function handleAsk(prompt) {
    setClipPopup(null);
    if (onAskWithContext) onAskWithContext(prompt);
  }

  if (!isDesktop) {
    return (
      <div className="flex flex-col items-center justify-center h-full bg-white text-gray-500 p-8 text-center">
        <Folder className="w-12 h-12 mb-3 opacity-40 text-gray-400" />
        <p className="text-sm">Local Files requires the AiNxt desktop app.</p>
        <p className="text-xs text-gray-400 mt-1">Download from Settings → Desktop App.</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-white text-gray-800">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">Local Workspace</h2>
          <p className="text-xs text-gray-500 mt-0.5">Add folders here · ask file questions in Chat · live sync</p>
        </div>
        <button onClick={addWorkspace}
          className="flex items-center gap-1.5 bg-indigo-600 hover:bg-indigo-700 text-white text-xs px-3 py-1.5 rounded-lg transition">
          <Folder className="w-3.5 h-3.5" />
          Add folder
        </button>
      </div>

      {/* MCP status */}
      <div className={`flex items-center gap-2 px-4 py-2 text-xs border-b border-gray-200 ${
        mcpStatus === "ok" ? "text-green-700 bg-green-50" :
        mcpStatus === "error" ? "text-yellow-700 bg-yellow-50" :
        "text-gray-500 bg-gray-50"
      }`}>
        {mcpStatus === "ok" ? <CheckCircle className="w-3.5 h-3.5" /> :
         mcpStatus === "error" ? <AlertCircle className="w-3.5 h-3.5" /> :
         <Zap className="w-3.5 h-3.5" />}
        {mcpStatus === "ok" && "Local MCP server registered — AiNxt can read your files"}
        {mcpStatus === "error" && `MCP: ${mcpError}`}
        {mcpStatus === "registering" && "Registering local MCP server…"}
        {!mcpStatus && "Local MCP server starting…"}
      </div>

      {/* Live change indicator */}
      {recentChange && (
        <div className="flex items-center gap-2 px-4 py-1.5 text-xs text-indigo-700 bg-indigo-50 border-b border-gray-200">
          <RefreshCw className="w-3 h-3 animate-spin" />
          Re-indexing: {recentChange}
        </div>
      )}

      {/* Workspace list */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {workspaces.length === 0 ? (
          <div className="text-center text-gray-500 mt-12 space-y-2">
            <FolderOpen className="w-10 h-10 mx-auto opacity-40 text-gray-400" />
            <p className="text-sm">No local workspaces added yet.</p>
            <p className="text-xs text-gray-400">Add a folder, then go to <strong className="text-gray-600">Chat</strong> and ask things like "list files in my Desktop" or "read package.json".</p>
          </div>
        ) : (
          workspaces.map(folder => (
            <WorkspaceRow
              key={folder}
              folder={folder}
              onRemove={f => setWorkspaces(prev => prev.filter(x => x !== f))}
              onAsk={handleAsk}
            />
          ))
        )}
      </div>

      {/* Clipboard intelligence popup */}
      <ClipboardPopup
        text={clipPopup}
        onAsk={handleAsk}
        onDismiss={() => setClipPopup(null)}
      />
    </div>
  );
}
