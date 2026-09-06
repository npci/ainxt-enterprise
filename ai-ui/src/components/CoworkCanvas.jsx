// SPDX-License-Identifier: MIT
// ============================================================
// CoworkCanvas — collaborative document canvas (Canvas/Pages parity)
//
// Surfaces the server-side iterative-editing engine in the UI:
//   - shows the version history of a document artifact (GET /docs/{id}/versions)
//   - renders the selected version's rendered pages (cookie-auth <img>)
//   - lets the user apply a natural-language AI edit that produces a NEW version
//     (POST /docs/{id}/revise → poll the build job → refresh)
//
// Self-contained: open with <CoworkCanvas artifactId=… onClose=… />. All calls
// go through authFetch (httpOnly cookie). No secrets logged.
// ============================================================
import { useState, useEffect, useCallback, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import { API_BASE, authFetch } from "../config";
import { mdComponents } from "./Message.jsx";
import { X, History, Wand2, FileDown, Loader2, Clock } from "lucide-react";
import DocLivePreview from "./DocLivePreview.jsx";

// Memoised formatter — constructed once, reused on every render.
// timeZoneName:"short" appends the viewer's local TZ label (e.g. "GMT+5:30")
// so the timestamp is self-describing without hardcoding any zone.
const _localTimeFmt = new Intl.DateTimeFormat(undefined, {
  day:          "2-digit",
  month:        "short",
  year:         "numeric",
  hour:         "2-digit",
  minute:       "2-digit",
  hour12:       true,
  timeZoneName: "short",
});

/**
 * Render a backend UTC timestamp in the viewer's local timezone.
 * Appends "Z" to bare strings so JS parses them as UTC, not local time.
 * @param {string|number|Date} ts
 * @returns {string}  e.g. "17 Aug 2026, 04:00 PM GMT+5:30"
 */
function formatLocalTime(ts) {
  if (!ts) return "—";
  try {
    const d =
      ts instanceof Date     ? ts :
      typeof ts === "number" ? new Date(ts) :
      new Date(/[Z+\-]\d{2}:?\d{2}$/.test(ts) ? ts : `${ts}Z`);
    return _localTimeFmt.format(d);
  } catch {
    return String(ts);
  }
}

export default function CoworkCanvas({ artifactId, onClose }) {
  const [data, setData]             = useState(null);   // {artifact_id,title,versions:[]}
  const [active, setActive]         = useState(null);   // selected version
  const [pageUrls, setPageUrls]     = useState([]);     // object URLs for the active version
  const [instruction, setInstr]     = useState("");
  const [loading, setLoading]       = useState(true);
  const [busy, setBusy]             = useState(false);  // a revision is building
  const [stage, setStage]           = useState("");
  const [err, setErr]               = useState("");
  // Live "see-as-you-go" generation state during an AI edit. livePreview holds
  // the incremental { title, sections[], done } snapshot the worker publishes;
  // genProgress holds the { label } build step. jobId is the in-flight revise
  // job so the Stop button can cancel it.
  const [livePreview, setLivePreview] = useState(null);
  const [genProgress, setGenProgress] = useState(null);
  const [jobId, setJobId]             = useState(null);
  const [cancelling, setCancelling]   = useState(false);
  // Synchronous flag the polling loop reads to distinguish a user Stop (clean
  // abort) from a genuine build failure — state updates aren't visible to the
  // running loop's closure.
  const cancelledRef                  = useRef(false);

  const load = useCallback(async () => {
    setErr("");
    try {
      const r = await authFetch(`${API_BASE}/docs/${artifactId}/versions`);
      if (!r.ok) throw new Error("Couldn't load version history");
      const d = await r.json();
      setData(d);
      setActive((prev) => d.versions?.find(v => v.version === prev?.version)
                          || d.versions?.[d.versions.length - 1] || null);
    } catch (e) { setErr(String(e.message || e)); }
    finally { setLoading(false); }
  }, [artifactId]);

  useEffect(() => { load(); }, [load]);

  // Render the active version's pages (auth-required → blobs → object URLs).
  useEffect(() => {
    if (!active?.file_id) { setPageUrls([]); return; }
    let cancelled = false;
    const urls = [];
    (async () => {
      // Preview page images (PDF/PPT via LibreOffice) render ASYNCHRONOUSLY and can
      // lag the "done" status by a few seconds. Previously the loop broke on the
      // first non-200 — so a not-yet-rendered page 1 left the canvas blank forever.
      // Now: retry page 1 a few times; once pages appear, walk until the first gap.
      const fetchPage = async (p) => {
        try {
          const r = await authFetch(`${API_BASE}/docs/preview/${active.file_id}/${p}`);
          if (!r.ok) return null;
          const blob = await r.blob();
          if (!blob || blob.size === 0 || !/image\//i.test(blob.type || "image/")) return null;
          return blob;
        } catch { return null; }
      };
      // Wait for page 1 (up to ~20s) before giving up to the markdown fallback.
      let first = null;
      for (let attempt = 0; attempt < 10 && !cancelled && !first; attempt++) {
        first = await fetchPage(1);
        if (!first) await new Promise((res) => setTimeout(res, 2000));
      }
      if (cancelled || !first) return;   // → component shows content_md fallback
      urls.push(URL.createObjectURL(first));
      setPageUrls([...urls]);
      for (let p = 2; p <= 50 && !cancelled; p++) {
        const blob = await fetchPage(p);
        if (!blob) break;                // no more pages
        urls.push(URL.createObjectURL(blob));
        setPageUrls([...urls]);
      }
    })();
    return () => { cancelled = true; urls.forEach(u => URL.revokeObjectURL(u)); };
  }, [active?.file_id]);

  const download = (fileId, fmt) => {
    authFetch(`${API_BASE}/docs/download/${fileId}`)
      .then(r => r.blob())
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = `${data?.title || "document"}.${fmt}`;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }).catch(() => {});
  };

  const applyEdit = async () => {
    const ins = instruction.trim();
    if (!ins || busy) return;
    setBusy(true); setErr(""); setStage("Applying your edit…");
    setLivePreview(null); setGenProgress(null); setJobId(null); setCancelling(false);
    cancelledRef.current = false;
    try {
      const r = await authFetch(`${API_BASE}/docs/revise`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artifact_id: artifactId, instruction: ins }),
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        throw new Error(e.detail || "Revision failed");
      }
      const { job_id } = await r.json();
      setJobId(job_id);
      setStage("Rebuilding the document…");
      const start = Date.now();
      while (Date.now() - start < 1800000) {            // 30-min ceiling
        await new Promise(res => setTimeout(res, 1500));
        const s = await authFetch(`${API_BASE}/docs/job/${job_id}/status`);
        const js = await s.json().catch(() => ({}));
        // Stream the worker's incremental snapshot into the live preview so the
        // user watches the document materialize section-by-section.
        if (js.live_preview) setLivePreview(js.live_preview);
        if (js.progress)     setGenProgress(js.progress);
        if (js.status === "done") { setInstr(""); await load(); break; }
        if (js.status === "error") {
          // A user-initiated Stop resolves the job with this sentinel — treat
          // it as a clean abort (no red error banner), not a build failure.
          const msg = js.error || "Build failed";
          if (cancelledRef.current || /cancel/i.test(msg)) break;
          throw new Error(msg);
        }
      }
    } catch (e) { setErr(String(e.message || e)); }
    finally {
      setBusy(false); setStage("");
      setLivePreview(null); setGenProgress(null); setJobId(null); setCancelling(false);
    }
  };

  // Stop an in-flight AI edit. Cancels the build job server-side; the polling
  // loop terminates when it next sees status === "error" ("Cancelled by user").
  const stopEdit = async () => {
    if (!jobId || cancelling) return;
    setCancelling(true);
    cancelledRef.current = true;
    try {
      await authFetch(`${API_BASE}/docs/job/${jobId}/cancel`, { method: "POST" });
    } catch { /* best-effort */ }
  };

  return (
    <div className="fixed inset-0 z-[120] bg-black/60 backdrop-blur-sm flex" onClick={onClose}>
      <div className="m-auto w-[min(1100px,95vw)] h-[min(88vh,900px)] bg-white rounded-2xl shadow-2xl
                      flex overflow-hidden" onClick={(e) => e.stopPropagation()}>
        {/* Left: version history */}
        <div className="w-60 shrink-0 border-r border-gray-200 bg-gray-50 flex flex-col">
          <div className="px-4 py-3 border-b border-gray-200 flex items-center gap-2 text-sm font-semibold text-gray-700">
            <History size={15} /> Versions
          </div>
          <div className="flex-1 overflow-auto p-2 space-y-1">
            {(data?.versions || []).slice().reverse().map((v) => (
              <button key={v.version} onClick={() => setActive(v)}
                className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-colors
                  ${active?.version === v.version
                    ? "bg-indigo-100 text-indigo-800 border border-indigo-200"
                    : "hover:bg-gray-100 text-gray-600 border border-transparent"}`}>
                <div className="font-medium">v{v.version} · {v.format}</div>
                <div className="flex items-center gap-1 text-[11px] text-gray-400 mt-0.5">
                  <Clock size={10} />{formatLocalTime(v.created_at)}
                </div>
              </button>
            ))}
            {!loading && !(data?.versions || []).length && (
              <div className="px-3 py-4 text-xs text-gray-400">No versions yet.</div>
            )}
          </div>
        </div>

        {/* Right: preview + edit */}
        <div className="flex-1 flex flex-col min-w-0">
          <div className="px-5 py-3 border-b border-gray-200 flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="text-sm font-semibold text-gray-800 truncate">
                {data?.title || "Document"} {active ? <span className="text-gray-400">· v{active.version}</span> : null}
              </div>
              <div className="text-[11px] text-gray-400">AiNxt Canvas · iterative AI editing</div>
            </div>
            <div className="flex items-center gap-2">
              {active && (
                <button onClick={() => download(active.file_id, active.format)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-200
                             hover:bg-gray-50 text-gray-700 text-sm cursor-pointer">
                  <FileDown size={14} /> Download
                </button>
              )}
              <button onClick={onClose} title="Close"
                className="p-1.5 rounded-lg hover:bg-gray-100 text-gray-500 cursor-pointer"><X size={18} /></button>
            </div>
          </div>

          <div className="flex-1 overflow-auto bg-gray-100 p-4">
            {busy ? (
              // See-as-you-go: the document materialises section-by-section
              // while the edit builds. Non-interactive — the only action is the
              // Stop button (via onCancel) so the user can abort if they want
              // to change the instruction.
              <div className="h-full flex items-start justify-center pt-2">
                <DocLivePreview
                  progress={genProgress}
                  livePreview={livePreview}
                  format={active?.format || data?.versions?.[0]?.format || "document"}
                  mode="edit"
                  onCancel={stopEdit}
                  cancelling={cancelling}
                />
              </div>
            ) : loading ? (
              <div className="h-full flex items-center justify-center text-gray-400 text-sm">
                <Loader2 size={16} className="animate-spin mr-2" /> Loading…
              </div>
            ) : pageUrls.length ? (
              <div className="mx-auto max-w-3xl space-y-3">
                {pageUrls.map((u, i) => (
                  <img key={i} src={u} alt={`Page ${i + 1}`}
                       className="w-full rounded-lg bg-white shadow-md border border-gray-200" />
                ))}
              </div>
            ) : active?.content_md ? (
              // No rasterized pages for this version — render the markdown source
              // so the Canvas always shows the document content. Use the SAME wrapper
              // + plugin set as the chat renderer (md-body, not `prose` — `prose`
              // fought the custom mdComponents styles and produced ugly raw-looking
              // text). This makes canvas markdown match chat exactly.
              <div className="mx-auto max-w-3xl">
                <div className="md-body rounded-lg bg-white shadow-md border border-gray-200 p-8">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm, remarkMath]}
                    rehypePlugins={[rehypeHighlight, rehypeKatex]}
                    components={mdComponents}>
                    {active.content_md}
                  </ReactMarkdown>
                </div>
              </div>
            ) : (
              <div className="h-full flex items-center justify-center text-gray-400 text-sm">
                No preview available for this version — use Download.
              </div>
            )}
          </div>

          {/* AI edit box */}
          <div className="border-t border-gray-200 p-3 bg-white">
            {err && <div className="mb-2 text-xs text-red-600">{err}</div>}
            <div className="flex items-end gap-2">
              <textarea
                value={instruction} onChange={(e) => setInstr(e.target.value)}
                disabled={busy}
                placeholder="Describe an edit — e.g. 'shorten the summary', 'add a risks section', 'make the tone formal', 'swap the cover image'…"
                rows={2}
                onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) applyEdit(); }}
                className="flex-1 resize-none rounded-xl border border-gray-300 px-3 py-2 text-sm
                           focus:outline-none focus:ring-2 focus:ring-indigo-300 disabled:bg-gray-50" />
              <button onClick={applyEdit} disabled={busy || !instruction.trim()}
                className="flex items-center gap-1.5 px-4 py-2.5 rounded-xl text-sm font-medium cursor-pointer
                           bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-default">
                {busy ? <Loader2 size={15} className="animate-spin" /> : <Wand2 size={15} />}
                {busy ? (stage || "Working…") : "Apply edit"}
              </button>
            </div>
            <div className="mt-1 text-[11px] text-gray-400">⌘/Ctrl+Enter to apply · each edit creates a new version (history kept)</div>
          </div>
        </div>
      </div>
    </div>
  );
}
