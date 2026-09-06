// SPDX-License-Identifier: MIT
// ============================================================
// ArtifactsPanel — Claude-Artifacts / ChatGPT-Canvas equivalent
//
// Renders a right-pane drawer with a sandboxed iframe containing
// the artifact's content. Three artifact types are previewable
// today:
//   html      — raw HTML
//   svg       — wrapped in an HTML body
//   mermaid   — rendered via the same mermaid module as chat
// Other types (markdown / code / react) fall back to a syntax-
// highlighted source view.
// ============================================================
import { useEffect, useMemo, useRef, useState } from "react";
import { X, Eye, Save } from "lucide-react";
import { API_BASE as API, authFetch } from "../config";

function buildIframeDoc(art) {
  if (art.type === "html") {
    return art.content;
  }
  if (art.type === "svg") {
    const svg = art.content.includes("<svg")
      ? art.content
      : `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600">${art.content}</svg>`;
    return `<!doctype html><html><body style="margin:0;padding:16px;font-family:system-ui">${svg}</body></html>`;
  }
  if (art.type === "mermaid") {
    return `<!doctype html><html><head><script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script></head><body style="margin:0;padding:16px;font-family:system-ui"><pre class="mermaid">${art.content
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")}</pre><script>mermaid.initialize({startOnLoad:true});</script></body></html>`;
  }
  // Code / markdown / react → preview as plain source for now
  return `<!doctype html><html><body style="margin:0;padding:16px;font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:pre-wrap">${(art.content || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")}</body></html>`;
}

export default function ArtifactsPanel({ artifactId, chatId, onClose }) {
  const [art, setArt]       = useState(null);
  const [tab, setTab]       = useState("preview"); // preview | source
  const [error, setError]   = useState("");
  const iframeRef           = useRef(null);

  useEffect(() => {
    let cancelled = false;
    setArt(null); setError("");
    if (!artifactId || !chatId) return;
    authFetch(`${API}/chats/${chatId}/artifacts/${artifactId}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error("Artifact load failed")))
      .then(data => { if (!cancelled) setArt(data); })
      .catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [artifactId, chatId]);

  const html = useMemo(() => (art ? buildIframeDoc(art) : ""), [art]);

  return (
    <div className="fixed inset-y-0 right-0 z-30 w-full max-w-2xl bg-white border-l border-gray-200 shadow-xl flex flex-col">
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[10px] font-semibold uppercase text-purple-600 tracking-wide">
            {art?.type || "artifact"}
          </span>
          <div className="text-sm font-medium text-gray-800 truncate">{art?.title || "Loading…"}</div>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setTab("preview")}
            className={`px-2 py-1 text-xs rounded ${tab === "preview" ? "bg-gray-900 text-white" : "text-gray-500 hover:bg-gray-100"}`}
          >
            <Eye size={12} className="inline mr-1" /> Preview
          </button>
          <button
            onClick={onClose}
            className="ml-1 p-1 rounded text-gray-500 hover:bg-gray-100"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-hidden bg-gray-50">
        {error && <div className="p-4 text-sm text-red-600">{error}</div>}
        {!error && !art && <div className="p-4 text-sm text-gray-400">Loading…</div>}
        {art && tab === "preview" && (
          <iframe
            ref={iframeRef}
            title={art.title}
            sandbox=""
            srcDoc={html}
            className="w-full h-full border-0"
          />
        )}
      </div>
    </div>
  );
}
