// SPDX-License-Identifier: MIT
// ============================================================
// AGENTS CATALOG
// Consumer-facing browse + inline chat view.
// - Public agents (visibility=public) and department-restricted agents
// - User-specific favorites (starred)
// - Clicking an agent opens an inline chat scoped to that agent
// ============================================================

import { useState, useEffect, useRef, useCallback } from "react";
import {
  Bot, Star, MessageSquare, Search, X, ChevronLeft,
  Paperclip, SendHorizontal, CirclePauseIcon, FileText,
  Loader2, Globe, Lock, CircleX, ShieldOff,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { mdComponents } from "./Message";
import { API_BASE as API, authFetch } from "../config";
import { useFileDrop } from "../hooks/useFileDrop";
import { toISTDate } from "../utils/time";
import { useToast } from "./ui/DialogProvider.jsx";
import AiNxtSpinner from "./AiNxtSpinner";
import { cacheStore } from "../utils/previewCache";

// ── Status badge colours ─────────────────────────────────────
const STATUS_COLORS = {
  PRODUCTION:        "bg-green-100 text-green-700",
  APPROVED:          "bg-blue-100 text-blue-700",
  PENDING_APPROVAL:  "bg-yellow-100 text-yellow-700",
  DRAFT:             "bg-gray-100 text-gray-500",
};

const ACCEPT_TYPES = "application/pdf,.docx,.xlsx,.csv,.txt,.json,.html,.htm,.rtf";
const IMAGE_TYPES  = ["image/jpeg", "image/png", "image/gif", "image/webp"];
const IMAGE_ACCEPTED = IMAGE_TYPES.join(",");

// ── Agent Card ────────────────────────────────────────────────
function AgentCard({ agent, isFavorited, onToggleFavorite, onChat }) {
  const tools   = agent.tools  || [];
  const skills  = agent.skills || [];
  const allTags = [...tools, ...skills];

  return (
    <div className="border border-gray-200  rounded-xl p-4 hover:bg-indigo-50 hover:shadow-sm transition bg-white group flex flex-col h-full">
      {/* Header */}
      <div className="flex items-start gap-3 mb-2">
        <div className="w-9 h-9 rounded-lg bg-indigo-50 flex items-center justify-center flex-shrink-0">
          <Bot size={18} className="text-indigo-500" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-gray-600 truncate">{agent.name}</p>
          <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded-full ${STATUS_COLORS[agent.status] || "bg-gray-100 text-gray-500"}`}>
            {agent.status}
          </span>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {agent.visibility === "public"
            ? <Globe size={11} className="text-gray-300" title="Public" />
            : <Lock size={11} className="text-gray-300" title="Department restricted" />
          }
          <button
            onClick={(e) => { e.stopPropagation(); onToggleFavorite(agent.name, isFavorited); }}
            className={`transition cursor-pointer ${isFavorited ? "text-amber-400 hover:text-amber-300" : "text-gray-300 hover:text-yellow-400"}`}
            title={isFavorited ? "Remove from favorites" : "Add to favorites"}
          >
            <Star size={14} fill={isFavorited ? "currentColor" : "none"} />
          </button>
        </div>
      </div>

      {/* Description — 2-line clamp, flex-1 pushes footer down */}
      <p className="text-xs text-gray-500 mb-1 line-clamp-2 leading-relaxed">
        {agent.description || "No description provided."}
      </p>
      {agent.created_at && (
        <p className="text-[10px] text-gray-400 mb-3">Created {toISTDate(agent.created_at)}</p>
      )}

      {/* Tool/skill tags — hidden in catalog view (internal plumbing, not meaningful to end users) */}
      {/* {allTags.slice(0, 3).map(t => (
        <span key={t} className="text-[10px] bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">
          {t.replace(/_/g, " ")}
        </span>
      ))}
      {allTags.length > 3 && <span className="text-[10px] text-gray-400">+{allTags.length - 3} more</span>} */}

      <div className="flex justify-end">
        <button
          onClick={() => onChat(agent)}
          className="text-xs border-indigo-600 bg-gradient-to-br from-indigo-600 to-violet-600 bg-clip-text text-transparent group-hover:underline font-medium flex items-center gap-1 cursor-pointer"
        >
          <MessageSquare size={11} />
          Chat →
        </button>
      </div>
    </div>
  );
}


// ── Agent Inline Chat ────────────────────────────────────────
function AgentChat({ agent, user, onClose }) {
  const { toast } = useToast();

  const [messages, setMessages]     = useState([]);
  const [input, setInput]           = useState("");
  const [loading, setLoading]       = useState(false);
  const [chatId]                    = useState(() => crypto.randomUUID());
  const [attachments, setAttachments] = useState([]);
  const [uploading, setUploading]   = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [fileLimitError, setFileLimitError] = useState(false);
  const [imageFile, setImageFile]   = useState(null);
  const [imagePreviewUrl, setImagePreviewUrl] = useState(null);
  const abortRef    = useRef(null);
  const bottomRef   = useRef(null);
  const fileInputRef = useRef(null);
  const uploadXhrRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Auto-dismiss the file-limit error banner after 5 seconds
  useEffect(() => {
    if (!fileLimitError) return;
    const t = setTimeout(() => setFileLimitError(false), 5000);
    return () => clearTimeout(t);
  }, [fileLimitError]);

  // Abort any in-flight upload if AgentChat unmounts mid-upload (prevents setState-after-unmount)
  useEffect(() => () => {
    if (uploadXhrRef.current) {
      uploadXhrRef.current.abort();
      uploadXhrRef.current = null;
    }
  }, []);

  // ── Paste image ──────────────────────────────────────────
  const handlePaste = useCallback((e) => {
    const items = Array.from(e.clipboardData?.items || []);
    const img = items.find(it => it.type.startsWith('image/'));
    if (!img) return;
    e.preventDefault();
    const blob = img.getAsFile();
    if (!blob) return;
    const file = new File([blob], `paste-${Date.now()}.png`, { type: blob.type });
    if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
    setImageFile(file);
    setImagePreviewUrl(URL.createObjectURL(file));
  }, [imagePreviewUrl]);

  // ── Drag-drop ────────────────────────────────────────────
  const { isDragging, dropHandlers } = useFileDrop({
    onFiles: (files) => {
      const images = files.filter(f => IMAGE_TYPES.includes(f.type));
      const docs   = files.filter(f => !IMAGE_TYPES.includes(f.type));
      if (images[0]) {
        if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
        setImageFile(images[0]);
        setImagePreviewUrl(URL.createObjectURL(images[0]));
      }
      if (docs.length) handleFileUpload({ target: { files: docs } });
    },
    disabled: loading,
  });

  function cancelUpload() {
    if (uploadXhrRef.current) {
      uploadXhrRef.current.abort();
      uploadXhrRef.current = null;
    }
    setUploading(false);
    setUploadProgress(0);
    toast?.info?.("Upload cancelled.");
  }

  async function handleFileUpload(e) {
    if (uploading) return; // prevent concurrent uploads from clobbering uploadXhrRef
    const files = Array.from(e.target?.files || []);
    if (!files.length) return;
    const MAX_FILES = 3;
    if (attachments.length + files.length > MAX_FILES) {
      setFileLimitError(true);
      if (e.target?.value !== undefined) e.target.value = "";
      return;
    }
    setUploading(true);
    setUploadProgress(0);
    if (e.target?.value !== undefined) e.target.value = "";

    const fd = new FormData();
    fd.append("chat_id", chatId);
    files.forEach(f => fd.append("files", f));

    try {
      const result = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        uploadXhrRef.current = xhr;
        xhr.open("POST", `${API}/chat/upload`);
        xhr.withCredentials = true; // sends httpOnly auth_token cookie
        xhr.upload.onprogress = (ev) => {
          if (ev.lengthComputable) setUploadProgress(Math.round((ev.loaded / ev.total) * 100));
        };
        xhr.onload = () => {
          uploadXhrRef.current = null;
          if (xhr.status >= 200 && xhr.status < 300) {
            try { resolve(JSON.parse(xhr.responseText)); }
            catch { reject(new Error("Invalid response")); }
          } else {
            reject(new Error(`Upload failed: ${xhr.status}`));
          }
        };
        xhr.onerror = () => { uploadXhrRef.current = null; reject(new Error("Network error")); };
        xhr.onabort = () => { uploadXhrRef.current = null; reject(new Error("Upload cancelled")); };
        xhr.send(fd);
      });
      const uploaded = result.uploaded || [];
      setAttachments(prev => [...prev, ...uploaded.filter(u => !u.blocked)]);

      for (const entry of uploaded) {
        if (entry.blocked) continue;
        const originalFile = files.find(f => f.name === entry.file_name);
        if (originalFile) {
          cacheStore(entry.id, originalFile, originalFile.type || "application/octet-stream");
        }
      }

      const blocked = uploaded.filter(u => u.blocked);
      if (blocked.length > 0) {
        const complianceMsgs = blocked.map(b => ({
          id:           crypto.randomUUID(),
          role:         "compliance_block",
          filename:     b.file_name,
          block_reason: b.block_reason || null,
          reasons:      b.compliance_reasons || [],
          streaming:    false,
        }));
        setMessages(prev => [...prev, ...complianceMsgs]);
      }
    } catch (err) {
      // Don't show an error card for intentional cancellations
      if (err.message !== "Upload cancelled") {
        setMessages(prev => [...prev, {
          id: crypto.randomUUID(), role: "assistant",
          content: `Upload error: ${err.message}`, streaming: false,
        }]);
      }
    } finally {
      uploadXhrRef.current = null;
      setUploading(false);
      setUploadProgress(0);
    }
  }

  async function sendMessage() {
    const text = input.trim();
    if (!text && !attachments.length && !imageFile) return;
    if (loading) return;

    // Snapshot before setAttachments([]) below — request body must reference IDs that we're about to clear.
    const pendingAttachments = attachments;

    const userMsg = {
      id: crypto.randomUUID(), role: "user", content: text,
      attachments: pendingAttachments.map(a => a.file_name),
    };
    const asstId  = crypto.randomUUID();
    const asstMsg = { id: asstId, role: "assistant", content: "", streaming: true };

    setMessages(prev => [...prev, userMsg, asstMsg]);
    setInput("");
    setAttachments([]);
    if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
    setImageFile(null);
    setImagePreviewUrl(null);
    setLoading(true);

    const controller = new AbortController();
    abortRef.current = controller;
    let buffer = "";

    try {
      const body = {
        message:        text,
        session_id:     chatId,
        stream:         true,
        attachment_ids: pendingAttachments.map(a => a.id),
      };
      const resp = await authFetch(`${API}/agents/${agent.name}/run`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
        signal:  controller.signal,
      });

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const parsed = JSON.parse(line.slice(6));
            if (parsed.__meta__) continue;
            if (parsed.t) {
              setMessages(prev => prev.map(m =>
                m.id === asstId ? { ...m, content: m.content + parsed.t } : m
              ));
            }
          } catch { /* ignore */ }
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        toast?.error?.("Failed to get response");
      }
    } finally {
      setMessages(prev => prev.map(m =>
        m.id === asstId ? { ...m, streaming: false } : m
      ));
      setLoading(false);
    }
  }

  function stopGeneration() {
    abortRef.current?.abort();
    setLoading(false);
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-3 border-b border-gray-100 bg-white flex-shrink-0">
        <button onClick={onClose} className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition">
          <ChevronLeft size={18} />
        </button>
        <div className="w-8 h-8 rounded-lg brand-grad-vivid flex items-center justify-center">
          <Bot size={16} className="text-white" />
        </div>
        <div>
          <p className="text-sm font-semibold text-gray-800">{agent.name}</p>
          <p className="text-xs text-gray-400 truncate max-w-[300px]">{agent.description}</p>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto overflow-x-hidden px-6 py-6 leading-5">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-center gap-3">
            <div className="w-12 h-12 rounded-full bg-indigo-50 flex items-center justify-center">
              <Bot size={22} className="text-indigo-400" />
            </div>
            <p className="text-sm font-medium text-gray-700">Chat with {agent.name}</p>
            <p className="text-xs text-gray-400 max-w-xs">{agent.description || "Ask me anything."}</p>
          </div>
        )}
        {messages.map(m => {
          if (m.role === "compliance_block") {
            const isComplianceViolation = m.reasons && m.reasons.length > 0;
            return (
              <div key={m.id} className="flex justify-start mb-4">
                <div className="max-w-md rounded-xl border border-red-200 bg-red-50 px-4 py-3">
                  <div className="flex items-center gap-2 mb-2">
                    <ShieldOff size={14} className="text-red-500 flex-shrink-0" />
                    <span className="text-xs font-semibold text-red-700">
                      {isComplianceViolation ? "File blocked by compliance policy" : "File type not supported"}
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5 mb-2">
                    <FileText size={11} className="text-red-400" />
                    <span className="text-xs text-red-600 font-medium truncate">{m.filename}</span>
                  </div>
                  {isComplianceViolation && (
                    <>
                      <div className="text-[10px] text-red-400 mb-1.5">Sensitive data detected:</div>
                      <div className="flex flex-wrap gap-1">
                        {m.reasons.map(r => (
                          <span key={r} className="bg-red-100 border border-red-200 text-red-700 text-[10px] font-semibold px-2 py-0.5 rounded">
                            {r}
                          </span>
                        ))}
                      </div>
                    </>
                  )}
                  <div className="text-[10px] text-red-400 mt-2">
                    {isComplianceViolation
                      ? "Remove the sensitive data from this file and upload again."
                      : (m.block_reason || "This file format cannot be uploaded.")}
                  </div>
                </div>
              </div>
            );
          }
          return (
          <div key={m.id} className={m.role === "user" ? "flex justify-end mb-6" : "flex justify-start mb-6"}>
            <div className={`${
              m.role === "user"
                ? "bg-gray-100 px-4 py-3 rounded-md text-sm max-w-4xl"
                : "px-4 py-3 rounded-md text-sm max-w-5xl"
            }`}>
              {m.role === "assistant" ? (
                <div className="md-body">
                  {m.content ? (
                    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={mdComponents}>
                      {m.content}
                    </ReactMarkdown>
                  ) : m.streaming ? (
                    <AiNxtSpinner />
                  ) : null}
                  {m.streaming && m.content && (
                    <span className="inline-flex gap-0.5 ml-1 align-middle">
                      <span className="w-1 h-1 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                      <span className="w-1 h-1 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                      <span className="w-1 h-1 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                    </span>
                  )}
                </div>
              ) : (
                <div className="whitespace-pre-wrap text-gray-800">{m.content}</div>
              )}
            </div>
          </div>
          );
        })}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div
        className={`border-t border-gray-100 bg-white px-4 pb-4 pt-3 flex-shrink-0 relative transition-all ${isDragging ? "bg-blue-50" : ""}`}
        {...dropHandlers}
      >
        {isDragging && (
          <div className="absolute inset-0 z-10 flex items-center justify-center rounded-b-xl border-2 border-dashed border-blue-400 bg-blue-50/80 pointer-events-none">
            <div className="flex flex-col items-center gap-1 text-blue-500">
              <Paperclip size={24} />
              <span className="text-sm font-medium">Drop files to attach</span>
            </div>
          </div>
        )}

        {/* Hidden file input */}
        <input ref={fileInputRef} type="file" multiple accept={ACCEPT_TYPES} onChange={handleFileUpload} className="hidden" />

        {/* Image preview */}
        {imagePreviewUrl && (
          <div className="mb-2 flex items-center gap-2">
            <div className="relative inline-block">
              <img src={imagePreviewUrl} alt="Preview" className="h-16 max-w-[120px] rounded object-contain border border-gray-200 bg-gray-50" />
              <button onClick={() => { URL.revokeObjectURL(imagePreviewUrl); setImageFile(null); setImagePreviewUrl(null); }}
                className="absolute -top-1 -right-1 bg-white border border-gray-300 rounded-full p-0.5 text-gray-500 hover:text-red-500 transition">
                <X size={9} />
              </button>
            </div>
          </div>
        )}

        {/* Attachment chips */}
        {attachments.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1">
            {attachments.map(a => (
              <div key={a.id} className="flex items-center gap-1 bg-blue-50 border border-blue-200 text-blue-700 text-xs px-2 py-0.5 rounded-full">
                <FileText size={10} />
                <span className="truncate max-w-[100px]">{a.file_name}</span>
                <button onClick={() => setAttachments(prev => prev.filter(x => x.id !== a.id))}>
                  <X size={10} />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Upload progress bar — visible only while uploading */}
        {uploading && (
          <div className="mb-2 flex items-center gap-2">
            <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-500 rounded-full transition-all duration-200"
                style={{ width: `${uploadProgress}%` }}
              />
            </div>
            <span className="text-xs text-blue-500 font-medium w-9 text-right shrink-0">
              {uploadProgress}%
            </span>
            <button
              onClick={cancelUpload}
              title="Cancel upload"
              className="shrink-0 flex items-center justify-center w-6 h-6 text-red-500 rounded-full hover:bg-red-50 transition cursor-pointer"
            >
              <CircleX size={16} />
            </button>
          </div>
        )}

        {/* File-count limit banner */}
        {fileLimitError && (
          <div className="mb-2 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            <span className="mt-0.5 shrink-0">⚠️</span>
            <span className="flex-1">
              The number of files you are trying to add exceeds the maximum limit. Ainxt currently supports adding up to 3 files at a time.
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

        <div className={`border rounded-xl bg-gray-50 transition-colors shadow-md ${loading ? "border-gray-200" : "border-gray-300 focus-within:border-indigo-300 focus-within:bg-white"}`}>
          <textarea
            value={input}
            disabled={loading}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); } }}
            onPaste={handlePaste}
            placeholder={`Ask ${agent.name} anything…`}
            rows={3}
            className="w-full resize-none bg-transparent px-3 pt-3 pb-1 outline-none text-sm text-gray-800 placeholder-gray-400"
          />
          <div className="flex items-center gap-1 px-2 pb-2 ">
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={loading || uploading}
              className="p-1.5 cursor-pointer text-gray-400 hover:text-gray-600 hover:bg-gray-200 rounded-lg transition disabled:opacity-40"
              title="Attach files"
            >
              {uploading ? <Loader2 size={16} className="animate-spin text-blue-500" /> : <Paperclip size={16} />}
            </button>
            <div className="flex-1" />
            <button
              onClick={loading ? stopGeneration : sendMessage}
              disabled={!loading && !input.trim() && !attachments.length}
              className="p-1.5 text-gray-700 cursor-pointer hover:text-gray-500 transition disabled:opacity-30"
            >
              {loading ? <CirclePauseIcon size={20} /> : <SendHorizontal size={20} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


// ── Main AgentsCatalog ───────────────────────────────────────
export default function AgentsCatalog({ user }) {
  const [catalog, setCatalog]       = useState({ public: [], department: [], favorites: [] });
  const [loading, setLoading]       = useState(true);
  const [search, setSearch]         = useState("");
  const [favorites, setFavorites]   = useState(new Set());
  const [selectedAgent, setSelectedAgent] = useState(null);

  useEffect(() => {
    fetchCatalog();
  }, []);

  async function fetchCatalog() {
    setLoading(true);
    try {
      const r = await authFetch(`${API}/agents/catalog`);
      if (r.ok) {
        const data = await r.json();
        setCatalog(data);
        setFavorites(new Set(data.favorites || []));
      }
    } catch { /* ignore */ } finally { setLoading(false); }
  }

  async function toggleFavorite(agentName, currently) {
    // Optimistic update
    setFavorites(prev => {
      const next = new Set(prev);
      currently ? next.delete(agentName) : next.add(agentName);
      return next;
    });
    try {
      await authFetch(`${API}/agents/${agentName}/favorite`, {
        method: currently ? "DELETE" : "POST",
      });
    } catch {
      // Revert on failure
      setFavorites(prev => {
        const next = new Set(prev);
        currently ? next.add(agentName) : next.delete(agentName);
        return next;
      });
    }
  }

  function filterAgents(list) {
    if (!search) return list;
    const q = search.toLowerCase();
    return list.filter(a =>
      a.name.toLowerCase().includes(q) ||
      (a.description || "").toLowerCase().includes(q)
    );
  }

  const pubAgents  = filterAgents(catalog.public  || []);
  const deptAgents = filterAgents(catalog.department || []);
  // Include favourited agents that fall outside the visible public/department
  // sets (server returns them in `favorite_agents`) so starred items always
  // render (#30). De-dupe by name.
  const _starredPool = [
    ...(catalog.public || []),
    ...(catalog.department || []),
    ...(catalog.favorite_agents || []),
  ];
  const _seenStar = new Set();
  const starredAll = _starredPool.filter((a) => {
    if (!favorites.has(a.name) || _seenStar.has(a.name)) return false;
    _seenStar.add(a.name);
    return true;
  });
  const starred    = filterAgents(starredAll);

  // ── Section ─────────────────────────────────────────────
  function Section({ title, icon: Icon, agents, emptyMsg, horizontal }) {
    if (!agents.length) return null;
    return (
      <div>
        <div className="flex items-center gap-2 mb-3">
          {Icon && <Icon size={14} className="text-gray-400" />}
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">{title}</h3>
          <span className="text-[10px] text-gray-400">({agents.length})</span>
        </div>
        <div className={
          horizontal
            ? "flex gap-3 overflow-x-auto pb-2"
            : "grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3"
        }>
          {agents.map(a => (
            <div key={a.name} className={horizontal ? "flex-shrink-0 w-56" : ""}>
              <AgentCard
                agent={a}
                isFavorited={favorites.has(a.name)}
                onToggleFavorite={toggleFavorite}
                onChat={setSelectedAgent}
              />
            </div>
          ))}
        </div>
      </div>
    );
  }

  // ── If agent selected — show inline chat ─────────────────
  if (selectedAgent) {
    return (
      <div className="flex flex-col h-full">
        <AgentChat
          agent={selectedAgent}
          user={user}
          onClose={() => setSelectedAgent(null)}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-gray-50">
      {/* Top bar */}
      <div className="flex items-center gap-3 px-6 py-4 border-b border-gray-200 bg-white flex-shrink-0">
        <Bot size={20} className="text-indigo-700" />
        <h1 className="text-sm font-semibold  text-indigo-700">Agents</h1>
        <div className="flex-1" />
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search agents…"
            className="pl-8 pr-4 py-1.5 text-sm border border-gray-200 rounded-lg  focus:outline-none focus:border-indigo-300 focus:bg-white w-56 shadow-sm"
          />
          {search && (
            <button onClick={() => setSearch("")} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
              <X size={12} />
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-6 py-5 space-y-8">
        {loading ? (
          <div className="flex items-center justify-center h-32 gap-2 text-gray-400">
            <Loader2 size={18} className="animate-spin" />
            <span className="text-sm">Loading agents…</span>
          </div>
        ) : (
          <>
            {/* Starred */}
            {starred.length > 0 && (
              <Section title="Starred" icon={Star} agents={starred} horizontal />
            )}

            {/* Public */}
            <Section
              title="Public Agents"
              icon={Globe}
              agents={pubAgents}
              emptyMsg="No public agents available."
            />

            {/* Department */}
            {deptAgents.length > 0 && (
              <Section
                title={`${user?.department || "Your Department"} Agents`}
                icon={Lock}
                agents={deptAgents}
              />
            )}

            {/* Empty state */}
            {!pubAgents.length && !deptAgents.length && (
              <div className="flex flex-col items-center justify-center h-48 text-center gap-3 text-gray-400">
                <Bot size={36} className="text-gray-300" />
                <p className="text-sm font-medium text-gray-500">
                  {search ? `No agents match "${search}"` : "No agents available yet"}
                </p>
                <p className="text-xs text-gray-400">
                  {search ? "Try a different search term" : "Agents in APPROVED or PRODUCTION status will appear here"}
                </p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
