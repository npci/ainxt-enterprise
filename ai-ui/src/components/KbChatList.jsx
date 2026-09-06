// SPDX-License-Identifier: MIT
// KbChatList — left-side chat history list for the Knowledge Base "Chat"
// tab. Visually mirrors the main Chat sidebar list (same row style, same
// hover actions, same edit-in-place rename) but only shows KB chats and
// stays inside the KB page (no SPA navigation).
//
// Props:
//   chats           — App-level chats array (NOT pre-filtered).
//   setChats        — App-level setter.
//   activeChatId    — currently active chat id (within the KB panel).
//   setActiveChatId — App-level setter. We call it with `null` to mean
//                     "no chat selected" so the right panel falls back
//                     to the drill-down picker.
//   onNewChat       — explicit "start a new KB chat" handler from the
//                     parent. Parent decides what "new" means (reset to
//                     drill-down picker).
//   chatsLoading    — App-level loading flag while /chats is fetched.

import { useState, useRef, useEffect } from "react";
import {
  MessageSquare, Plus, Pencil, Trash2,
} from "lucide-react";
import { API_BASE, authFetch } from "../config";
import { useConfirm, useToast } from "./ui/DialogProvider.jsx";
import { isKbChat } from "../utils/kbChat.js";
import { formatIstStamp, formatKbScope } from "../utils/kbFormat.js";

export default function KbChatList({
  chats,
  setChats,
  activeChatId,
  setActiveChatId,
  onNewChat,
  chatsLoading = false,
  pickerVisible = false,
}) {
  const { confirm } = useConfirm();
  const { toast }   = useToast();

  const [editingId,    setEditingId]    = useState(null);
  const [editingTitle, setEditingTitle] = useState("");
  const inputRef = useRef(null);

  // Filter down to KB chats only. Sort: pinned first, then most recently
  // updated. We sort here (not in App state) so the main Chat sidebar
  // can keep its own ordering.
  const kbChats = (chats || []).filter(isKbChat).slice().sort((a, b) => {
    if ((b.pinned ? 1 : 0) !== (a.pinned ? 1 : 0)) return (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0);
    return (b.updatedAt || 0) - (a.updatedAt || 0);
  });

  useEffect(() => {
    if (editingId && inputRef.current) inputRef.current.focus();
  }, [editingId]);

  function startRename(chat) {
    setEditingId(chat.id);
    setEditingTitle(chat.title || "");
  }

  async function saveRename(chatId) {
    const title = (editingTitle || "").trim() || "New KB Chat";
    setEditingId(null);
    setChats(prev => prev.map(c => c.id === chatId ? { ...c, title } : c));
    try {
      await authFetch(`${API_BASE}/chats/${chatId}/title`, {
        method:  "PATCH",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ title }),
      });
    } catch {
      toast.error("Failed to rename chat");
    }
  }

  async function deleteChat(chat) {
    // Build a richer confirm body that identifies the chat by its scope
    // and creation timestamp (IST). Older chats without a populated
    // scope payload fall back gracefully to the title-only line.
    const chatTitle = chat.title || "New KB Chat";
    const scopeStr = formatKbScope({
      product_name: chat._kb_scope_labels?.productName || chat.product_name,
      domain:       chat.domain,
      spec_version: chat.spec_version,
    });
    const createdAt = chat.createdAt || chat.created_at || chat.updatedAt;
    const createdStr = createdAt ? formatIstStamp(createdAt) : "";

    const ok = await confirm({
      title:        "Delete chat?",
      message:
        `“${chatTitle}” will be permanently deleted.\n` +
        `Scope: ${scopeStr}` +
        (createdStr ? ` · Created: ${createdStr}` : ""),
      confirmLabel: "Delete",
      variant:      "danger",
    });
    if (!ok) return;

    // Optimistic: drop locally first. If the API call fails we restore.
    const prev = chats;
    const next = chats.filter(c => c.id !== chat.id);
    setChats(next);
    if (chat.id === activeChatId) setActiveChatId(null);

    try {
      const res = await authFetch(`${API_BASE}/chats/${chat.id}`, { method: "DELETE" });
      if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
    } catch {
      setChats(prev);
      toast.error("Failed to delete chat");
    }
  }

  // ── Render ─────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col h-full">
      {/* Header — just the "New" action. The page-level "Knowledge Base"
          title sits in KnowledgeBase.jsx's shared title bar; we don't
          repeat it here. */}
      <div className="flex-shrink-0 px-3 py-2 border-b border-gray-100 flex items-center justify-end">
        <button
          type="button"
          onClick={onNewChat}
          disabled={pickerVisible}
          title={pickerVisible ? "Scope picker is already open" : "Start a new KB chat"}
          className={`flex items-center gap-1 text-[11px] px-2 py-1 rounded-md transition ${
            pickerVisible
              ? "bg-gray-300 text-gray-500 cursor-not-allowed"
              : "bg-indigo-600 text-white hover:bg-indigo-700 cursor-pointer"
          }`}
        >
          <Plus size={11} />
          New
        </button>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto py-1">
        {chatsLoading && (
          <div className="px-4 py-6 text-xs text-gray-400 text-center">
            Loading…
          </div>
        )}

        {!chatsLoading && kbChats.length === 0 && (
          <div className="px-4 py-8 text-xs text-gray-400 text-center">
            No KB chats yet.
            <br />
            Click <span className="font-medium">New</span> to start one.
          </div>
        )}

        {!chatsLoading && kbChats.map(chat => {
          const isActive = chat.id === activeChatId;
          return (
            <div
              key={chat.id}
              onClick={() => setActiveChatId(chat.id)}
              className={`
                group relative flex items-center gap-2
                px-3 py-2 mx-1 rounded-md cursor-pointer mb-0.5 transition
                ${isActive
                  ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-l-indigo-500"
                  : "text-gray-600 hover:bg-gray-100 hover:text-gray-800"}
              `}
            >
              {editingId === chat.id ? (
                <input
                  ref={inputRef}
                  value={editingTitle}
                  onChange={e => setEditingTitle(e.target.value)}
                  onBlur={() => saveRename(chat.id)}
                  onKeyDown={e => {
                    if (e.key === "Enter") saveRename(chat.id);
                    if (e.key === "Escape") setEditingId(null);
                  }}
                  onClick={e => e.stopPropagation()}
                  className="bg-white border border-gray-300 rounded px-1 outline-none text-sm flex-1 min-w-0"
                />
              ) : (
                <>
                  <MessageSquare size={13} className="flex-shrink-0 text-gray-400" />
                  {/* Slightly compact font + native tooltip — large enough
                      to stay readable, small enough that long scope-encoded
                      titles ("Product · Domain · Version — timestamp") fit
                      more of the string in-line. Hover reveals the full
                      untruncated name. */}
                  <span
                    className="text-[13px] leading-tight truncate flex-1 min-w-0 pr-1"
                    title={chat.title || "New KB Chat"}
                  >
                    {chat.title || "New KB Chat"}
                  </span>
                  <div
                    className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition"
                    style={{ background: "inherit" }}
                  >
                    <button
                      type="button"
                      onClick={e => { e.stopPropagation(); startRename(chat); }}
                      title="Rename"
                      className="p-1 rounded text-indigo-700 hover:text-indigo-500 hover:bg-indigo-200 cursor-pointer"
                    >
                      <Pencil size={11} />
                    </button>
                    <button
                      type="button"
                      onClick={e => { e.stopPropagation(); deleteChat(chat); }}
                      title="Delete"
                      className="p-1 rounded text-red-500 hover:text-red-700 hover:bg-red-100 cursor-pointer"
                    >
                      <Trash2 size={11} />
                    </button>
                  </div>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
