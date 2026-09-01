// SPDX-License-Identifier: Apache-2.0
// ============================================================
// MemoryPanel — ChatGPT-style "Memories"
//
// Shows the cross-chat summaries AiNxt has saved about the user
// (from postgres_memory.save_user_memory) and lets the user
// delete individual entries or clear them all.
// Backed by:
//   GET    /memory/user
//   DELETE /memory/user/{id}
//   DELETE /memory/user
// ============================================================
import { useEffect, useState } from "react";
import { API_BASE as API, authFetch } from "../config";
import { Brain, Trash2, X, AlertTriangle } from "lucide-react";

function fmt(ts) {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export default function MemoryPanel({ onClose }) {
  const [entries, setEntries]   = useState([]);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState("");
  const [busyId, setBusyId]     = useState(null);
  const [clearing, setClearing] = useState(false);

  async function load() {
    setLoading(true); setError("");
    try {
      const r = await authFetch(`${API}/memory/user`);
      if (!r.ok) throw new Error("Failed to load memories");
      const d = await r.json();
      setEntries(Array.isArray(d?.entries) ? d.entries : []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function deleteEntry(id) {
    setBusyId(id);
    try {
      await authFetch(`${API}/memory/user/${id}`, { method: "DELETE" });
      setEntries(prev => prev.filter(e => e.id !== id));
    } catch (_e) { /* swallow */ }
    finally { setBusyId(null); }
  }

  async function clearAll() {
    if (!window.confirm("Forget everything AiNxt has remembered across chats? This cannot be undone.")) return;
    setClearing(true);
    try {
      await authFetch(`${API}/memory/user`, { method: "DELETE" });
      setEntries([]);
    } catch (_e) { /* swallow */ }
    finally { setClearing(false); }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-end bg-black/30 backdrop-blur-sm">
      <div className="bg-white w-full max-w-md h-full shadow-2xl flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <div className="flex items-center gap-2">
            <Brain size={18} className="text-purple-600" />
            <div>
              <div className="text-sm font-semibold text-gray-800">Memories</div>
              <div className="text-[11px] text-gray-500">What AiNxt remembers about you across chats</div>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-gray-100 text-gray-500"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-2">
          {loading && <div className="text-xs text-gray-500">Loading…</div>}
          {error && (
            <div className="flex items-center gap-2 text-xs text-red-600 bg-red-50 rounded-lg p-2">
              <AlertTriangle size={14} /> {error}
            </div>
          )}
          {!loading && !error && entries.length === 0 && (
            <div className="text-xs text-gray-400 italic">
              Nothing remembered yet. As you chat, AiNxt may distill durable facts here so it can
              keep context across new conversations.
            </div>
          )}
          {entries.map(e => (
            <div key={e.id} className="border border-gray-200 rounded-lg p-3 group hover:border-gray-300">
              <div className="text-sm text-gray-800 whitespace-pre-wrap">{e.content}</div>
              <div className="mt-1 flex items-center justify-between">
                <div className="text-[10px] text-gray-400">{fmt(e.created_at)}</div>
                <button
                  onClick={() => deleteEntry(e.id)}
                  disabled={busyId === e.id}
                  className="opacity-0 group-hover:opacity-100 transition text-gray-400 hover:text-red-500 p-1"
                  title="Forget this"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>

        {entries.length > 0 && (
          <div className="border-t border-gray-100 p-3">
            <button
              onClick={clearAll}
              disabled={clearing}
              className="w-full text-xs px-3 py-2 rounded-lg border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-40"
            >
              {clearing ? "Clearing…" : "Forget everything"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
