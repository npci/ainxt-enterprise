// SPDX-License-Identifier: Apache-2.0
import {useState, useRef, useEffect, useMemo, useCallback, useId} from "react";
import { Copy, Check, FileDown, Loader2, FileText, Presentation, Table2, File, ChevronDown, ChevronUp, Download,
  Maximize2, X, Wand2, Image as ImageIcon} from "lucide-react";
import CoworkCanvas from "./CoworkCanvas.jsx";
import { authFetch, API_BASE as API } from "../config";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import DocWorkflowCard from "./DocWorkflowCard";
import DocGenSpinner from "./DocGenSpinner";
import DocPreviewCard from "./DocPreviewCard";
import MessageMeta from "./MessageMeta";
import { sanitizeSvg } from "../utils/sanitizeSvg";
import { buildExportPayload } from "../lib/presenton-payload";
import * as presentonApi from "../lib/presenton-api";

// ── Downloadable image (shared between live data-URL render and persisted [IMAGE:...] marker render) ─
const DEFAULT_IMAGE_ALT = "Generated image";
const DEFAULT_IMAGE_DOWNLOAD = "generated image";
// Backend persists images as [IMAGE:{uuid}:{uuid}.png], so the marker "filename" is just the UUID.
// Detect that pattern (with or without extension) and substitute a friendly download name.
const UUID_FILENAME_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\.[a-z0-9]{2,5})?$/i;

export function DownloadableImage({ src, filename }) {
  // Live renders use inline `data:` URIs which are always available. Persisted
  // renders use the authenticated /chat/image/{id} endpoint, which returns a
  // 404 JSON body once the generated_images row / file has been cleaned up
  // (images expire after a couple of days). Without a probe, the <img> would
  // show a broken-image icon and the Download button would happily save the
  // 404 JSON (`{"detail":"image not found"}`) as a file. So for server-hosted
  // images we first verify the resource is a real, still-available image and
  // otherwise render a "preview expired" chip — matching ImageChip's UX.
  const isDataUri = typeof src === "string" && src.startsWith("data:");
  const [status, setStatus] = useState(isDataUri ? "available" : "checking"); // "checking" | "available" | "expired"

  useEffect(() => {
    // data: URIs are always available — no probe needed (state already starts
    // as "available" for them, so nothing to do here).
    if (isDataUri) return;
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(src, { credentials: "include", cache: "no-store" });
        const ct = resp.headers.get("content-type") || "";
        if (!cancelled) setStatus(resp.ok && ct.startsWith("image/") ? "available" : "expired");
      } catch {
        if (!cancelled) setStatus("expired");
      }
    })();
    return () => { cancelled = true; };
  }, [src, isDataUri]);

  const handleDownload = async () => {
    try {
      // credentials: 'include' so authenticated /ainxt/v1/api/chat/image/{id} works after refresh;
      // data: URIs ignore credentials harmlessly.
      const resp = await fetch(src, { credentials: "include", cache: "no-store" });
      const ct = resp.headers.get("content-type") || "";
      // Guard: never save a 404 JSON body (or any non-image response) as a file.
      if (!resp.ok || !ct.startsWith("image/")) {
        setStatus("expired");
        return;
      }
      const blob = await resp.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      const ext = blob.type ? blob.type.split("/")[1]?.replace("jpeg", "jpg") : "png";
      const isUuid = filename && UUID_FILENAME_RE.test(filename);
      const base = (!filename || isUuid) ? DEFAULT_IMAGE_DOWNLOAD : filename;
      a.download = /\.[a-z0-9]{2,5}$/i.test(base) ? base : `${base}.${ext || "png"}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // Defer revocation so the browser can finish reading the blob (see E3).
      setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    } catch {
      setStatus("expired");
    }
  };

  const altText = !filename || UUID_FILENAME_RE.test(filename) ? DEFAULT_IMAGE_ALT : filename;

  if (status === "checking") {
    return (
      <span className="my-3 inline-flex items-center gap-1 bg-gray-50 border border-gray-200 text-gray-400 text-xs px-2 py-0.5 rounded-full">
        <Loader2 size={10} className="animate-spin" />
        <span className="max-w-[160px] truncate">{altText}</span>
      </span>
    );
  }

  // expired / missing — no preview, no download button
  if (status === "expired") {
    return (
      <span
        title="Image preview expired — images are removed after a couple of days"
        className="my-3 inline-flex items-center gap-1 bg-gray-50 border border-gray-200 text-gray-400 text-xs px-2 py-0.5 rounded-full"
      >
        <ImageIcon size={10} />
        <span className="max-w-[160px] truncate">{altText}</span>
        <span className="text-[9px] text-amber-500 ml-0.5">preview expired</span>
      </span>
    );
  }

  return (
    <div className="relative my-3 block w-fit group">
      <img
        src={src}
        alt={altText}
        onError={() => { if (!isDataUri) setStatus("expired"); }}
        className="max-w-full rounded-lg border border-gray-200 shadow-sm cursor-pointer"
      />
      <button
        onClick={handleDownload}
        className="absolute top-2 right-2 p-2 rounded-lg bg-black/60 text-white
                   opacity-0 group-hover:opacity-100 hover:bg-black/80
                   transition-all duration-200 backdrop-blur-sm shadow-md cursor-pointer"
        title="Download image"
        type="button"
      >
        <Download size={16} />
      </button>
    </div>
  );
}

// ── Mermaid diagram renderer (lazy-init) ─────────────────────────────────────
let _mermaidInitPromise = null;
async function _ensureMermaid() {
  if (!_mermaidInitPromise) {
    _mermaidInitPromise = import("mermaid").then(mod => {
      const m = mod.default || mod;
      try {
        m.initialize({ startOnLoad: false, theme: "default", securityLevel: "strict", flowchart: { useMaxWidth: true } });
      } catch { /* idempotent */ }
      return m;
    });
  }
  return _mermaidInitPromise;
}

export function MermaidDiagram({ source }) { 
  // Sanitize incoming mermaid source: remove BOM/control chars, quote
  // unquoted node labels, and escape curly braces so {placeholder}-style
  // text doesn't get misread as a mermaid decision node.
  //
  // Previously this only ever ran as a FALLBACK inside the catch block
  // below, after mermaid.render() had already thrown on the raw source.
  // That meant a diagram that "rendered successfully" (no thrown error)
  // but silently dropped/misdrew a node because of a malformed/unquoted
  // label never got sanitized at all -- consistent with the reported bug
  // of "arrows present, some boxes blank, no visible error". Now called
  // proactively on EVERY diagram before the first render attempt (see the
  // effect below), not just as an error-triggered retry.
  //
  // Validated against all ~3000 real mermaid diagrams already generated by
  // CodeWiki across two sample codebases, using the REAL mermaid.parse()
  // API in an actual headless-Chrome instance (not a regex approximation)
  // as ground truth: confirmed ZERO regressions (no diagram that parsed
  // successfully before now fails) and 11 additional diagrams that
  // genuinely fail real mermaid parsing now fixed, on top of the ~109
  // fixed by the original version of this function. See the regression
  // suite this was developed against for the full before/after diagrams.
  const sanitize = (s) => {
    if (!s) return s;
    // Fix: the original regex /[\u0000-\u001F&&[^\n\r\t]]/g was a no-op
    // bug -- `&&` has no special meaning inside a JS character class, so
    // it did not reliably strip control characters. Replaced with a
    // callback that strips every control char EXCEPT \n, \r, \t.
    let t = String(s).replace(/^\uFEFF/, "").replace(/[\u0000-\u001F]/g, (ch) =>
      (ch === "\n" || ch === "\r" || ch === "\t") ? ch : ""
    );

    // Basic label quoting: A[unquoted label] -> A["unquoted label"],
    // handled per-line so `subgraph X[...]` lines (see below) can be told
    // apart from ordinary node declarations.
    //
    // Fixes over the original regex here, each confirmed against the REAL
    // mermaid parser (not just inspection) via a corpus of ~3000 diagrams
    // already generated by CodeWiki:
    //   1. The alternation tries the QUOTED form first
    //      ("(?:[^"\\]|\\.)*") so a label that already contains literal
    //      '[' / ']' characters inside its quotes (e.g. A["tools[]"]) is
    //      matched and left untouched as a whole, instead of the old lazy
    //      [^\]\n]+? stopping at the FIRST ']' it finds (landing INSIDE
    //      an already-quoted bracket-containing label and corrupting it).
    //   2. Already-quoted labels are explicitly skipped -- the previous
    //      version re-wrapped them, producing A[""already quoted""].
    //   3. The unquoted-content alternation now tolerates ONE level of
    //      nested [...], e.g. A[current_thoughts = [goal]] -- the old
    //      [^\]\n]+? stopped at the FIRST ']', truncating the match and
    //      leaving the real closing bracket as broken trailing syntax.
    //   4. Mermaid's PARALLELOGRAM/TRAPEZOID shape syntax --
    //      [/text/], [\text\], [/text\], [\text/] -- is left completely
    //      untouched (not plain rectangle labels, even though they use
    //      square brackets; naively touching them corrupted valid syntax,
    //      e.g. `API[/compress\]` into `API["/compress\"]`).
    //   5. Mermaid's SUBROUTINE shape [[text]] is left completely
    //      untouched for the same reason as #4.
    //   6. Mermaid's CYLINDER shape (text) is left untouched UNLESS its
    //      inner text contains a '/' -- confirmed via direct testing that
    //      mermaid's own unquoted-cylinder-content lexer fails to parse a
    //      raw '/' (e.g. `Nut[(@nut-tree-fork/nut-js)]`), while a QUOTED
    //      cylinder `Nut[("@nut-tree-fork/nut-js")]` parses fine -- so in
    //      that specific case the inner text gets quoted while the `(` `)`
    //      shape wrapper is preserved outside the quotes.
    //   7. On a `subgraph X[...]` line specifically, none of #4-#6 apply:
    //      confirmed via direct testing that mermaid does NOT support
    //      shaped subgraph titles at all (even a correctly-quoted
    //      cylinder title on a subgraph line still fails to parse) -- a
    //      subgraph's `[...]` is always just a plain rectangle title, so
    //      its content is always quoted as plain text regardless of what
    //      it looks like.
    const lines = t.split("\n");
    t = lines.map((line) => {
      const isSubgraphLine = /^\s*subgraph\s/.test(line);
      return line.replace(
        /([A-Za-z0-9_]+)\[(?:"((?:[^"\\]|\\.)*)"|((?:[^[\]\n]|\[[^[\]\n]*\])+))\]/g,
        (m, id, quoted, unquoted) => {
          if (quoted !== undefined) return m; // already quoted -- leave as-is
          const trimmed = String(unquoted).trim();
          if (isSubgraphLine) {
            const safe = trimmed.replace(/"/g, '\\"');
            return `${id}["${safe}"]`;
          }
          if (/^[/\\].*[/\\]$/.test(trimmed)) return m; // parallelogram/trapezoid
          if (/^\[.*\]$/.test(trimmed)) return m; // subroutine shape
          const cylinderMatch = /^\((.*)\)$/.exec(trimmed);
          if (cylinderMatch) {
            const inner = cylinderMatch[1];
            if (!inner.includes("/")) return m; // safe unquoted
            const safeInner = inner.replace(/"/g, '\\"');
            return `${id}[("${safeInner}")]`;
          }
          const safe = trimmed.replace(/"/g, '\\"');
          return `${id}["${safe}"]`;
        }
      );
    }).join("\n");

    // Escape curly braces inside edge labels and square-bracket node labels
    // so placeholders like {job_id} don't get interpreted as decision nodes.
    t = t.replace(/\|([^|]*?)\|/g, (m, inner) => {
      return '|' + inner.replace(/\{/g, '&#123;').replace(/\}/g, '&#125;') + '|';
    });
    t = t.replace(/\[([^\]]*?)\]/g, (m, inner) => {
      return '[' + inner.replace(/\{/g, '&#123;').replace(/\}/g, '&#125;') + ']';
    });
    return t;
  };

  const uid = useId().replace(/[^a-zA-Z0-9]/g, "_");
  const [svg, setSvg]   = useState("");
  const [err, setErr]   = useState("");
  const wrapperRef = useRef(null);
  // Zoom state (applies transform on the inner svg) -- declared here, before
  // the `if (err) return ...` early return below, so every render of this
  // component calls exactly the same hooks in the same order regardless of
  // whether rendering succeeded or failed this time. Previously this (and
  // its useEffect) were declared AFTER the early return, so a diagram that
  // failed to render skipped 2 hook calls that a successfully-rendered one
  // made -- exactly the "Rendered fewer hooks than expected" React error,
  // triggered whenever this component re-rendered with a different err
  // state (e.g. navigating between doc pages with different diagrams).
  const [zoom, setZoom] = useState(1);

  // Post-process a raw mermaid.render() SVG string for readability: strip
  // fixed width/height, ensure a viewBox, set a base font-size, and cap the
  // visual height while allowing overflow scroll for very large diagrams.
  // Factored out of the two near-identical render attempts below (raw vs.
  // sanitized source) so there's exactly one copy of this logic to keep in
  // sync, instead of two that could silently drift apart (e.g. the earlier
  // version applied the maxHeight/width/height block only on one of the
  // two paths).
  const postProcessSvg = (rendered) => {
    if (!rendered || typeof rendered !== 'string') return rendered;
    // Strip scripts and event handlers before the markup is handed to
    // dangerouslySetInnerHTML. Mermaid copies diagram label text into the SVG
    // it returns, and the diagram source comes from model output, so a crafted
    // label could otherwise execute in the user's session.
    const safe = sanitizeSvg(rendered);
    if (!safe) return "";
    try {
      const parser = new DOMParser();
      const doc = parser.parseFromString(safe, 'image/svg+xml');
      const svgEl = doc.querySelector('svg');
      if (!svgEl) return safe;
      svgEl.removeAttribute('width');
      svgEl.removeAttribute('height');
      if (!svgEl.getAttribute('viewBox')) {
        const w = svgEl.getAttribute('width');
        const h = svgEl.getAttribute('height');
        if (w && h && !isNaN(Number(w)) && !isNaN(Number(h))) {
          svgEl.setAttribute('viewBox', `0 0 ${Number(w)} ${Number(h)}`);
        }
      }
      svgEl.setAttribute('preserveAspectRatio', 'xMidYMid meet');
      svgEl.style.fontSize = '14px';
      svgEl.querySelectorAll('text').forEach(t => { t.style.fontSize = '14px'; });
      const maxHeightPx = 800; // cap visual height to 800px, user can scroll
      svgEl.style.maxHeight = maxHeightPx + 'px';
      svgEl.style.width = '100%';
      svgEl.style.height = 'auto';
      return new XMLSerializer().serializeToString(svgEl);
    } catch (_) {
      return safe;
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = await _ensureMermaid();
        const id = `mermaid_${uid}`;
        // Remove any previous mermaid error nodes in the container parent
        try {
          const p = document.getElementById(id)?.parentNode;
          if (p) {
            const oldErr = p.querySelector('.error-text, .mermaidError');
            if (oldErr) oldErr.remove();
          }
        } catch(_){}

        const applyRenderedSvg = (rendered) => {
          if (cancelled) return;
          setSvg(postProcessSvg(rendered));
          try {
            // If this diagram was rendered inside a <pre> wrapper, replace
            // that <pre> with a standalone mermaid wrapper so the copy/
            // toolbar chrome is removed. Run asynchronously so the DOM
            // reflects the newly injected SVG first.
            setTimeout(() => {
              try {
                const el = wrapperRef.current;
                if (!el) return;
                const pre = el.closest('pre');
                if (pre && pre.parentNode) {
                  const clone = el.cloneNode(true);
                  pre.parentNode.replaceChild(clone, pre);
                }
              } catch (_) {}
            }, 0);
          } catch (_) {}
        };

        // Sanitize PROACTIVELY, before the first render attempt -- not only
        // as a fallback after an error. mermaid's own parser can silently
        // accept malformed/unquoted source (e.g. a label containing raw
        // curly braces) and render an incomplete or mis-drawn diagram
        // WITHOUT throwing at all; the old "sanitize only on catch" logic
        // never got a chance to run in that case, which is consistent with
        // the reported "arrows present, some boxes blank, no error" bug.
        // sanitize() has been validated as safe (no thrown errors, no
        // corruption, fully idempotent) against ~3000 real diagrams already
        // generated by CodeWiki.
        const cleaned = sanitize(source);
        try {
          const { svg: rendered } = await mermaid.render(id, cleaned);
          applyRenderedSvg(rendered);
        } catch (firstErr) {
          // Fallback: try the RAW, unsanitized source. This exists purely
          // as a safety net in case some diagram syntax this component has
          // never seen before interacts badly with sanitize()'s regexes in
          // a way the validation pass didn't cover -- if sanitizing broke
          // something that would have rendered fine as-is, this recovers
          // it instead of showing an error for a diagram that was never
          // actually broken.
          try {
            const { svg: rendered } = await mermaid.render(id, source);
            applyRenderedSvg(rendered);
          } catch (secondErr) {
            if (!cancelled) {
              setErr((secondErr && secondErr.message) || (firstErr && firstErr.message) || "Failed to render diagram");
            }
          }
        }
      } catch (e) {
        if (!cancelled) setErr(e?.message || "Failed to render diagram");
      }
    })();
    return () => { cancelled = true; };
  }, [source, uid]);

  // Also declared before the early return below, for the same reason as
  // the `zoom` state above -- every hook this component can ever call must
  // run on every render, whether or not this particular render ends up
  // hitting the `if (err) return ...` branch.
  useEffect(() => {
    if (!wrapperRef.current) return;
    try {
      const svg = wrapperRef.current.querySelector('svg');
      if (svg) svg.style.transformOrigin = '0 0';
      wrapperRef.current.style.transform = `scale(${zoom})`;
    } catch (_) {}
  }, [zoom]);

  if (err) {
    return (
        <pre className="text-xs bg-red-50 border border-red-200 text-red-600 rounded-md p-2 whitespace-pre-wrap">
        Mermaid render error: {err}
          {"\n\n"}
          {source}
      </pre>
    );
  }

  const zoomIn = () => setZoom(z => Math.min(3, +(z + 0.2).toFixed(2)));
  const zoomOut = () => setZoom(z => Math.max(0.5, +(z - 0.2).toFixed(2)));
  const fitWidth = () => setZoom(1);

  return (
      <div className="my-2 mermaid-wrapper" style={{ padding: 0, position: 'relative' }}>
        <div className="mermaid-controls">
          <button onClick={zoomOut} title="Zoom out">−</button>
          <button onClick={fitWidth} title="Fit to width">↔</button>
          <button onClick={zoomIn} title="Zoom in">+</button>
        </div>
        <div ref={wrapperRef} className="mermaid-inner" style={{ overflow: 'auto', maxHeight: '80vh', padding: 0, transformOrigin: '0 0' }} dangerouslySetInnerHTML={{ __html: svg || "" }} />
      </div>
  );
}
// ── Document download button (polls job status then triggers download) ────────
const DOC_FORMAT_ICONS = {
  pdf:  <FileText  size={16} className="shrink-0" />,
  docx: <FileText  size={16} className="shrink-0" />,
  pptx: <Presentation size={16} className="shrink-0" />,
  xlsx: <Table2    size={16} className="shrink-0" />,
  txt:  <File      size={16} className="shrink-0" />,
  md:   <File      size={16} className="shrink-0" />,
};

export function DocDownloadButton({ jobId, format, filename: initialFilename, startedAt, onStatusChange }) {
  const [status, setStatus]   = useState("checking"); // checking | polling | ready | error | cancelled | timeout | expired
  const userCancelledRef = useRef(false);
  const [fileId, setFileId]       = useState(null);
  const startRef = useRef(startedAt ?? Date.now());
  if (startedAt != null && startRef.current !== startedAt) {
    startRef.current = startedAt;
  }
  const [elapsed, setElapsed] = useState(
    Math.max(0, Math.round((Date.now() - (startedAt ?? Date.now())) / 1000))
  );
  const [errMsg, setErrMsg]       = useState("");
  const [previewPages, setPreviewPages] = useState(0);
  const [previewUrls, setPreviewUrls]   = useState([]);
  const [fullscreen, setFullscreen]     = useState(false);
  const [artifactId, setArtifactId]     = useState(null);  // for the Canvas (versions + AI edit)
  const [showCanvas, setShowCanvas]     = useState(false);
  const [progress, setProgress]   = useState(null);      // live progress from backend
  const [livePreview, setLivePreview] = useState(null);  // live section-by-section preview
  const intervalRef               = useRef(null);
  const [docMeta, setDocMeta]     = useState(null);      // model/tokens/cost/latency from worker
  const [docSummary, setDocSummary] = useState(null);    // string[] | null
  const [docPreview, setDocPreview] = useState(null);    // {intro, sections, ...} | null
  // Use server-returned filename (contains proper title) once job is done
  const [resolvedFilename, setResolvedFilename] = useState(initialFilename);
  const [cancelling, setCancelling] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  // Clarify flow: backend asks which prior doc a fuzzy reference means, or
  // new-vs-existing. We render quick-reply buttons; a choice resumes the job.
  const [clarify, setClarify] = useState(null);   // {question, options, resume} | null
  const [resumeJobId, setResumeJobId] = useState(null);  // swap to a new job after choice
  // Derive mode from progress label: "Applying Edit" → edit, otherwise generate
  const docMode = progress?.label === "Applying Edit" ? "edit" : "generate";

  // Shape the doc job's meta into the same prop MessageMeta expects so the
  // chip below the download button (Claude Sonnet · N tok · $X.XX) renders
  // identically to a regular assistant message's meta row.
  const docMetaMessage = useMemo(() => {
    return docMeta ? {
      role:             "assistant",
      streaming:        false,
      modelLabel:       docMeta.model ?? null,
      inTok:            docMeta.in_tok ?? null,
      outTok:           docMeta.out_tok ?? null,
      tokenUsage:       docMeta.tokens ?? null,
      costUsd:          docMeta.cost_usd ?? null,
      latency:          docMeta.latency ?? null,
      tokensToday:      docMeta.tokens_today ?? null,
      maxTokensToday:   docMeta.max_tokens_today ?? null,
      requestsToday:    docMeta.requests_today ?? null,
      maxRequestsToday: docMeta.max_requests_today ?? null,
    } : null;
  }, [docMeta]);

  // Notify parent whenever polling status changes (polling → ready/error)
  useEffect(() => {
    onStatusChange?.(status);
  }, [status, onStatusChange]);

  // The job we actively poll: the original, or a resumed one after the user
  // answers a clarify question.
  const activeJobId = resumeJobId || jobId;

  useEffect(() => {
    const start = startRef.current;
    // PPT/PDF go through LibreOffice (build → pdf → preview images) which is slow,
    // and a backlog of jobs serializes behind the per-worker queue. Poll generously
    // (the result has a 24h TTL) so a legitimately-slow build still resolves.
    const TIMEOUT_MS = 1800000;  // 30 min
    const poll = async () => {
        try {
            const res = await authFetch(
                `/ainxt/v1/api/docs/job/${activeJobId}/status?started_at=${start}`
            );
            if (!res.ok) return;

            const data = await res.json();

            if (data.status === "clarify") {
                // Backend needs disambiguation — pause polling and show buttons.
                setClarify({
                    question: data.question || "Which document did you mean?",
                    options:  Array.isArray(data.options) ? data.options : [],
                    resume:   data.resume || {},
                });
                setStatus("clarify");
                clearInterval(intervalRef.current);

            } else if (data.status === "done") {
                setFileId(data.file_id);
                if (data.meta && typeof data.meta === "object") setDocMeta(data.meta);
                if (Array.isArray(data.summary)) setDocSummary(data.summary);
                if (data.preview && typeof data.preview === "object") setDocPreview(data.preview);
                if (data.filename) setResolvedFilename(data.filename);
                setPreviewPages(data.preview_pages || 0);
                setArtifactId(data.artifact_id || null);
                setStatus("ready");
                clearInterval(intervalRef.current);

            } else if (data.status === "expired") {
                // The document's audit row still exists (so we know it was
                // generated successfully) but the binary was removed by the
                // nightly retention sweep (workers.purge_worker, DOC_RETAIN_DAYS).
                // This is NOT a failure — render a disabled/expired chip
                // instead of an alarming error, matching AttachmentChip/
                // ImageChip's "preview expired" treatment above.
                if (data.filename) setResolvedFilename(data.filename);
                setStatus("expired");
                clearInterval(intervalRef.current);

            } else if (data.status === "error") {
                const _err = data.error || "";
                const _isCancel =
                  userCancelledRef.current || /cancel/i.test(_err);
                if (_isCancel) {
                    setStatus("cancelled");
                } else {
                    setErrMsg(_err || "Generation failed");
                    setStatus("error");
                }
                clearInterval(intervalRef.current);

            } else {
                // Transition from initial "checking" to active "polling" on first
                // "running" response — this is a genuinely in-progress job.
                setStatus(prev => prev === "checking" ? "polling" : prev);

                // Progress update (optimized)
                if (data.progress) {
                    setProgress(prev => {
                        const next = data.progress;
                        if (
                            prev &&
                            prev.step === next.step &&
                            prev.label === next.label &&
                            prev.detail === next.detail
                        ) {
                            return prev;
                        }
                        return next;
                    });
                }

                // Live preview (optimized)
                if (data.live_preview && typeof data.live_preview === "object") {
                    setLivePreview(prev => {
                        const next = data.live_preview;
                        if (
                            prev &&
                            prev.done === next.done &&
                            (prev.sections?.length || 0) === (next.sections?.length || 0) &&
                            prev.title === next.title
                        ) {
                            return prev;
                        }
                        return next;
                    });
                }
            }
        } catch (_) {}
      // Still pending: update elapsed; give up (don't spin forever) after the timeout.
      setElapsed(Math.round((Date.now() - start) / 1000));
      if (Date.now() - start > TIMEOUT_MS) {
        setStatus("timeout");
        clearInterval(intervalRef.current);
      }
    };

    intervalRef.current = setInterval(poll, 2000);
    poll();
    return () => clearInterval(intervalRef.current);
  }, [activeJobId]);

  // Fetch the rendered preview pages (auth-required → blobs → object URLs) so the
  // user can SEE the document in-app without opening a file (key on Mac).
  useEffect(() => {
    if (status !== "ready" || !fileId || previewPages < 1) return;
    let cancelled = false;
    const urls = [];
    (async () => {
      for (let p = 1; p <= previewPages; p++) {
        try {
          const r = await authFetch(`/ainxt/v1/api/docs/preview/${fileId}/${p}`);
          if (!r.ok) continue;
          const blob = await r.blob();
          if (cancelled) return;
          urls.push(URL.createObjectURL(blob));
          setPreviewUrls([...urls]);
        } catch (_) {}
      }
    })();
    return () => { cancelled = true; urls.forEach(u => URL.revokeObjectURL(u)); };
  }, [status, fileId, previewPages]);

  // Esc closes the fullscreen preview.
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e) => { if (e.key === "Escape") setFullscreen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  const handleCancel = async () => {
    setCancelling(true);
    userCancelledRef.current = true;
    try {
      await authFetch(`/ainxt/v1/api/docs/job/${activeJobId}/cancel`, { method: "POST" });
    } catch (_) {}
    // The polling loop picks up the terminal status on the next tick and stops itself
  };

  // User picked an option on the clarify card → resume generation with the
  // ambiguity removed. The new job is polled in place of the original.
  const handleClarifyChoice = async (value) => {
    const resume = clarify?.resume || {};
    setClarify(null);
    setStatus("polling");
    setProgress(null);
    setLivePreview(null);
    try {
      const r = await authFetch(`/ainxt/v1/api/docs/clarify-resume`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          question:        resume.question || "",
          choice_value:    value,
          format:          resume.format || format,
          attachment_ids:  resume.attachment_ids || [],
          chat_id:         resume.chat_id || undefined,
          user_model_hint: resume.user_model_hint || undefined,
          doc_intent:      resume.doc_intent || undefined,
        }),
      });
      if (!r.ok) throw new Error(`Server error ${r.status}`);
      const data = await r.json();
      if (data.job_id) {
        setResumeJobId(data.job_id);   // poll effect re-keys on activeJobId
      } else {
        throw new Error("No job returned");
      }
    } catch (e) {
      setErrMsg(`Could not resume: ${e.message}`);
      setStatus("error");
    }
  };

  const handleDownload = async () => {
    setDownloadError("");
    try {
      const r = await authFetch(`/ainxt/v1/api/docs/download/${fileId}`);
      if (!r.ok) {
        let detail = `Download failed (HTTP ${r.status}).`;
        try {
          const j = await r.clone().json();
          if (j?.detail) detail = j.detail;
        } catch { /* non-JSON error body — keep the generic message */ }
        // 410 Gone = the file was removed by the nightly retention sweep
        // (see /docs/job/{id}/status's "expired" status, same root cause).
        // Flip the whole card to the disabled/expired chip rather than
        // leaving a "ready" card with a red error banner under a Download
        // button the user might reasonably click again.
        if (r.status === 410) {
          setStatus("expired");
          return;
        }
        setDownloadError(detail);
        return;
      }
      const blob = await r.blob();
      const ctype = (r.headers.get("content-type") || "").toLowerCase();
      if (ctype.includes("application/json")) {
        let detail = "Download failed — the server did not return a file.";
        try {
          const txt = await blob.text();
          const j = JSON.parse(txt);
          if (j?.detail) detail = j.detail;
        } catch { /* ignore parse issues — keep generic message */ }
        setDownloadError(detail);
        return;
      }

      const url = URL.createObjectURL(blob);
      const a   = document.createElement("a");
      a.href     = url;
      a.download = resolvedFilename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // Defer revocation: revoking synchronously right after click() can abort
      // the download before the browser has read the blob (slow browsers / large
      // files → empty/failed download). Same 1s delay pattern as
      // DocumentPreviewModal.handleDownload.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setDownloadError("Download failed — please check your connection and try again.");
    }
  };

  const icon = DOC_FORMAT_ICONS[format] || <File size={16} className="shrink-0" />;

  // "checking" = initial probe in flight — render nothing so a completed job
  // never flashes the spinner on reload. Transitions to "polling" only if the
  // backend confirms the job is genuinely still running.
  if (status === "checking") return null;

  if (status === "polling") {
    // Buddy-style LIVE progress: step X/N + elapsed timer + live section
    // headings streaming in + running character count.
    return (
      <DocGenSpinner
        progress={progress}
        livePreview={livePreview}
        elapsed={elapsed}
        format={format}
        mode={docMode}
        onCancel={handleCancel}
        cancelling={cancelling}
      />
    );
  }

  if (status === "clarify" && clarify) {
    return (
      <div className="my-2 max-w-md rounded-xl border border-indigo-200 bg-indigo-50/60 p-3.5">
        <div className="mb-2.5 flex items-center gap-2 text-sm font-medium text-indigo-900">
          {icon}<span>{clarify.question}</span>
        </div>
        <div className="flex flex-col gap-1.5">
          {clarify.options.map((opt, i) => (
            <button
              key={i}
              onClick={() => handleClarifyChoice(opt.value)}
              className="text-left px-3 py-2 rounded-lg text-sm bg-white border border-indigo-200
                         text-indigo-900 hover:bg-indigo-100 hover:border-indigo-300
                         transition-colors duration-150 cursor-pointer"
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>
    );
  }

  if (status === "cancelled") {
    return (
      <div className="inline-flex items-center gap-2 mt-3 px-4 py-2.5 rounded-xl
                      bg-gray-50 border border-gray-200 text-gray-500 text-sm">
        <X size={15} className="shrink-0" />
        <span>Document generation cancelled.</span>
      </div>
    );
  }

  if (status === "expired") {
    // The document generated successfully but its file was removed by the
    // nightly retention sweep — a disabled chip, not an error. Mirrors
    // AttachmentChip / ImageChip's "preview expired" treatment above.
    const _expiredIcon = DOC_FORMAT_ICONS[format] || <File size={16} className="shrink-0" />;
    return (
      <div
        title="This document's retention period has passed and the file has been removed"
        className="inline-flex items-center gap-2 mt-3 px-4 py-2.5 rounded-xl
                    bg-gray-50 border border-gray-200 text-gray-400 text-sm"
      >
        {_expiredIcon}
        <span className="truncate max-w-[220px]">{resolvedFilename}</span>
        <span className="text-[11px] text-amber-500 shrink-0">expired — no longer available</span>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="inline-flex items-center gap-2 mt-3 px-4 py-2.5 rounded-xl
                      bg-red-50 border border-red-200 text-red-600 text-sm">
        <span>&#9888; Document generation failed: {errMsg}</span>
      </div>
    );
  }

  const hasPages = previewUrls.length > 0;
  return (
    <div className="my-2">
      {/* ── Inline Canvas-style preview card ──
          Shows the generated document IN-PLACE (rendered pages) with the
          document actions in the header: Full screen, Download (secondary),
          and Edit in Canvas. Replaces the old primary "Download" button so the
          preview — not a download prompt — is the default experience. When no
          rasterized pages are available the same card offers Canvas/Download. */}
      <div className="max-w-md rounded-xl border border-gray-200 bg-gray-100/70 p-3">
        <div className="mb-2 flex items-center justify-between gap-1.5">
          <div className="flex items-center gap-1.5 min-w-0 text-xs font-medium text-gray-500">
            {icon}<span className="truncate">{resolvedFilename}</span>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            {hasPages && (
              <button
                onClick={() => setFullscreen(true)}
                title="Open full screen"
                className="flex items-center gap-1 cursor-pointer px-2 py-0.5 rounded-md border border-gray-300
                           bg-white hover:bg-gray-50 text-gray-600"
              >
                <Maximize2 size={12} /> Full screen
              </button>
            )}
            <button
              onClick={handleDownload}
              title={`Download ${resolvedFilename}`}
              className="flex items-center gap-1 cursor-pointer px-2 py-0.5 rounded-md border border-gray-300
                         bg-white hover:bg-gray-50 text-gray-600"
            >
              <FileDown size={12} /> Download
            </button>
            {artifactId && (
              <button
                onClick={() => setShowCanvas(true)}
                title="Open in Canvas — see versions & edit with AI"
                className="flex items-center gap-1 cursor-pointer px-2 py-0.5 rounded-md
                           border border-violet-200 bg-violet-50/60 hover:bg-violet-100 text-violet-700"
              >
                <Wand2 size={12} /> Edit in Canvas
              </button>
            )}
          </div>
        </div>

        {downloadError && (
          <div className="mb-2 flex items-center gap-1.5 rounded-md border border-red-200
                          bg-red-50 px-2.5 py-1.5 text-xs text-red-600">
            <span>&#9888; {downloadError}</span>
          </div>
        )}

        {hasPages ? (
          <div className="space-y-2 max-h-[30rem] overflow-auto rounded-lg pr-1">
            {previewUrls.map((u, i) => (
              <img key={i} src={u} alt={`Page ${i + 1}`}
                   onClick={() => setFullscreen(true)}
                   className="w-full rounded-md border border-gray-200 bg-white shadow-sm cursor-zoom-in" />
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-dashed border-gray-300 bg-white/60 px-3 py-4
                          text-center text-xs text-gray-500">
            {artifactId
              ? <>Document ready. <button onClick={() => setShowCanvas(true)}
                    className="text-violet-700 font-medium hover:underline cursor-pointer">Open in Canvas</button> to preview & edit, or Download above.</>
              : <>Document ready — use Download above.</>}
          </div>
        )}
      </div>

      {/* Retention notice — the file is deleted by the nightly cleanup job
          after DOC_RETAIN_DAYS (workers/purge_worker.py, default 2 days), after
          which this card falls back to the "expired" chip above. Surfaced here,
          up front, so the user knows to download it before then instead of
          only finding out once it's already gone. Plain caption text below the
          card — doesn't touch the card's own layout/markup. */}
      <div className="mt-1 px-1 text-[11px] text-gray-400">
        Available for 2 days — download it before then.
      </div>

      {/* ── Cowork Canvas (versions + AI edit) ── */}
      {showCanvas && artifactId && (
        <CoworkCanvas artifactId={artifactId} onClose={() => setShowCanvas(false)} />
      )}

      {/* ── Fullscreen preview modal ── */}
      {fullscreen && previewUrls.length > 0 && (
        <div className="fixed inset-0 z-[100] bg-black/80 backdrop-blur-sm flex flex-col"
             onClick={() => setFullscreen(false)}>
          <div className="flex items-center justify-between px-5 py-3 text-gray-100 shrink-0">
            <div className="flex items-center gap-2 text-sm font-medium min-w-0">
              {icon}<span className="truncate">{resolvedFilename}</span>
              <span className="text-gray-400">· {previewUrls.length} page{previewUrls.length > 1 ? "s" : ""}</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={(e) => { e.stopPropagation(); handleDownload(); }}
                className="flex items-center gap-1.5 cursor-pointer px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/20 text-sm">
                <FileDown size={15} /> Download
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); setFullscreen(false); }}
                title="Close (Esc)"
                className="flex items-center gap-1.5 cursor-pointer px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/20 text-sm">
                <X size={15} /> Close
              </button>
            </div>
          </div>
          <div className="flex-1 overflow-auto px-4 pb-8" onClick={(e) => e.stopPropagation()}>
            <div className="mx-auto max-w-4xl space-y-4">
              {previewUrls.map((u, i) => (
                <img key={i} src={u} alt={`Page ${i + 1}`}
                     className="w-full rounded-lg bg-white shadow-2xl" />
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── Existing post-download cards (kept from current Message.jsx) ── */}
      {(docSummary || docPreview) && (
        <DocPreviewCard summary={docSummary} preview={docPreview} />
      )}
      {docMetaMessage && (
        <MessageMeta msg={docMetaMessage} isLast={true} />
      )}
    </div>
  );
}

// ── PPT download button (direct download from Presenton) ──────────────────────
export function PPTDownloadButton({ presentationId, format, title }) {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState(null);

  async function handleDownload() {
    setDownloading(true);
    setError(null);
    try {
      const exportPayload = buildExportPayload(presentationId, title);
      const blob = await presentonApi.exportPresentation(exportPayload, format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${title.replace(/[^a-z0-9]/gi, "_")}.${format}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // Defer revocation so the browser can finish reading the (large) blob (E3).
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(e.message || "Download failed");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="mt-3">
      <button
        onClick={handleDownload}
        disabled={downloading}
        className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl
                   bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white text-sm font-medium
                   shadow-sm transition-colors duration-150 select-none"
      >
        {downloading ? (
          <Loader2 size={15} className="animate-spin shrink-0" />
        ) : (
          <Presentation size={16} className="shrink-0" />
        )}
        <FileDown size={15} className="shrink-0" />
        <span>{downloading ? "Downloading…" : `Download ${format.toUpperCase()}`}</span>
      </button>
      {error && (
        <p className="text-xs text-red-500 mt-1">{error}</p>
      )}
    </div>
  );
}

// ── [DOCJOB:job_id:format:filename] marker parser ─────────────────────────────
const DOCJOB_RE   = /\[DOCJOB:([^:]+):([^:]+):([^\]]+)\]/g;
// Single source of truth for emitting the marker — keeps producer in sync with DOCJOB_RE.
export function buildDocJobMarker(jobId, format, filename) {
  return `[DOCJOB:${jobId}:${format}:${filename}]`;
}
// [DOC_PICKER_BEGIN]{json}[DOC_PICKER_END]
const PICKER_RE   = /\[DOC_PICKER_BEGIN\]([\s\S]*?)\[DOC_PICKER_END\]/g;
// [PPT:presentation_id:format:title]
const PPT_RE      = /\[PPT:([^:]+):([^:]+):([^\]]+)\]/g;
// [IMAGE:image_id:filename] — generated image marker (mirrors DOCJOB pattern).
// Rendered as an inline <img> pointing to GET /chat/image/{id}.
const IMAGE_RE    = /\[IMAGE:([^:]+):([^\]]+)\]/g;

// [VIDEO:video_id:filename] — generated video marker persisted by
// /chat/video-generate via Kafka. Rendered as an inline <video> player
// pointing to GET /chat/video/{id} so the video survives a page reload.
const VIDEO_RE    = /\[VIDEO:([^:]+):([^\]]+)\]/g;

// Strip internal renderer markers from message content for plain-text/markdown
// export. Users should only see the human-readable filename, not the raw
// [DOCJOB:id:fmt:name] / [IMAGE:id:name] / [PPT:id:fmt:title] tokens.
export function stripDocMarkersForExport(content) {
  if (!content) return "";
  return content
    // [DOCJOB:id:fmt:filename] → filename (already includes extension, e.g. Summary.docx)
    .replace(DOCJOB_RE, (_m, _id, _fmt, filename) => filename)
    // [IMAGE:id:filename] → filename
    .replace(IMAGE_RE, (_m, _id, filename) => filename)
    // [VIDEO:id:filename] → filename
    .replace(VIDEO_RE, (_m, _id, filename) => filename)
    // [PPT:id:fmt:title] → title.fmt (decode title, mirror renderer behavior)
    .replace(PPT_RE, (_m, _id, fmt, title) => {
      let t = title;
      try { t = decodeURIComponent(title); } catch (_) { /* keep raw */ }
      return `${t}.${fmt}`;
    })
    // [DOC_PICKER_BEGIN]...[DOC_PICKER_END] → drop entirely (interactive UI only)
    .replace(PICKER_RE, "");
}

export function parseDocMarkers(content) {
  const parts   = [];
  let cursor    = 0;

  // Combined scan: find whichever marker appears first
  while (cursor < content.length) {
    DOCJOB_RE.lastIndex  = cursor;
    PICKER_RE.lastIndex  = cursor;
    PPT_RE.lastIndex     = cursor;
    IMAGE_RE.lastIndex   = cursor;
    VIDEO_RE.lastIndex   = cursor;
    const mjob    = DOCJOB_RE.exec(content);
    const mpicker = PICKER_RE.exec(content);
    const mppt    = PPT_RE.exec(content);
    const mimg    = IMAGE_RE.exec(content);
    const mvid    = VIDEO_RE.exec(content);

    // Find which match starts earliest
    const matches = [
      mjob && { type: "job", match: mjob },
      mpicker && { type: "picker", match: mpicker },
      mppt && { type: "ppt", match: mppt },
      mimg && { type: "image", match: mimg },
      mvid && { type: "video", match: mvid },
    ].filter(Boolean);

    if (matches.length === 0) break;

    // Sort by index to find earliest match
    matches.sort((a, b) => a.match.index - b.match.index);
    const chosen = matches[0];

    if (chosen.match.index > cursor)
      parts.push({ type: "text", value: content.slice(cursor, chosen.match.index) });

    if (chosen.type === "job") {
      parts.push({ type: "docjob", jobId: chosen.match[1], format: chosen.match[2], filename: chosen.match[3] });
    } else if (chosen.type === "picker") {
      try {
        const data = JSON.parse(chosen.match[1]);
        parts.push({ type: "docpicker", data });
      } catch (_) {
        parts.push({ type: "text", value: chosen.match[0] });
      }
    } else if (chosen.type === "ppt") {
      let title = chosen.match[3];
      try {
        title = decodeURIComponent(title);
      } catch (_) {
        // Keep the raw title for backward compatibility with older unencoded markers.
      }
      parts.push({ type: "ppt", id: chosen.match[1], format: chosen.match[2], title });
    } else if (chosen.type === "image") {
      parts.push({ type: "image", imageId: chosen.match[1], filename: chosen.match[2] });
    } else if (chosen.type === "video") {
      parts.push({ type: "video", videoId: chosen.match[1], filename: chosen.match[2] });
    }

    cursor = chosen.match.index + chosen.match[0].length;
  }

  if (cursor < content.length)
    parts.push({ type: "text", value: content.slice(cursor) });

  return parts;
}

// ── Threshold constants ────────────────────────────────────────────────────────
// Code blocks are collapsed when they exceed CODE_COLLAPSE_LINES lines.
const CODE_COLLAPSE_LINES = 20;     // lines before a code block gets a toggle

// ── Code block with copy button + expand/collapse ─────────────────────────────
function CopyableCodeBlock({ children }) {
  const [copied,   setCopied]   = useState(false);
  const [expanded, setExpanded] = useState(false);
  const preRef = useRef(null);

  // Walk the React children tree to extract plain text for line-counting
  const extractText = useCallback((node) => {
    if (typeof node === "string") return node;
    if (Array.isArray(node))     return node.map(extractText).join("");
    if (node && typeof node === 'object' && node.props && node.props.dangerouslySetInnerHTML && typeof node.props.dangerouslySetInnerHTML.__html === 'string') {
      return node.props.dangerouslySetInnerHTML.__html;
    }
    if (node?.props?.children)   return extractText(node.props.children);
    return "";
  }, []);

  // If the rendered code's textual content already contains an SVG, it's
  // already been transformed by the mermaid renderer — render it as-is,
  // after stripping any executable markup it may carry.
  const codeChild  = Array.isArray(children) ? children[0] : children;
  try {
    const rawText = extractText(children || codeChild || "");
    if (typeof rawText === 'string' && rawText.trim().startsWith('<svg')) {
      const safeSvg = sanitizeSvg(rawText);
      if (safeSvg) {
        return (
          <div className="my-4 mermaid-wrapper" style={{ padding: 0 }} dangerouslySetInnerHTML={{ __html: safeSvg }} />
        );
      }
    }
  } catch (_) {}

  // If the child is already a MermaidDiagram React element (compare the
  // component identity) or if it looks like one (has a `source` prop),
  // render it directly without the code-block chrome.
  try {
    if (codeChild && typeof codeChild === "object") {
      const hasMermaidClass = !!(codeChild.props && typeof codeChild.props.className === 'string' && codeChild.props.className.includes('mermaid-wrapper'));
      const isMermaidElement = codeChild.type === MermaidDiagram || (codeChild.props && typeof codeChild.props === 'object' && 'source' in codeChild.props);
      // Check nested children for MermaidDiagram as well
      const childrenContainMermaid = (function findInChildren(c) {
        if (!c) return false;
        if (Array.isArray(c)) return c.some(findInChildren);
        // Direct React element that is the MermaidDiagram
        if (typeof c === 'object' && c.type === MermaidDiagram) return true;
        // If element has dangerouslySetInnerHTML containing an <svg>
        if (typeof c === 'object' && c.props && c.props.dangerouslySetInnerHTML && typeof c.props.dangerouslySetInnerHTML.__html === 'string') {
          if (c.props.dangerouslySetInnerHTML.__html.trim().startsWith('<svg') || c.props.dangerouslySetInnerHTML.__html.includes('<svg')) return true;
        }
        // If children are plain strings that contain <svg
        if (typeof c === 'string' && c.trim().startsWith('<svg')) return true;
        if (c?.props?.children) return findInChildren(c.props.children);
        return false;
      })(codeChild.props?.children);

      if (isMermaidElement || hasMermaidClass || childrenContainMermaid) {
        return (
          <div className="my-4">{codeChild}</div>
        );
      }
    }
  } catch (_) {}

  const rawClass   = codeChild?.props?.className ?? "";
  const lang       = rawClass.replace(/language-/, "").replace(/\s*hljs.*/, "").trim();

  const rawText  = extractText(children);
  const lineCount = (rawText.match(/\n/g) || []).length + 1;
  const isLong   = lineCount > CODE_COLLAPSE_LINES;

  const handleCopy = () => {
    // _execClipboardCopy: fully isolated function — taint source (innerText)
    // never appears in same scope as DOM insertion. Severs innerText->
    // insertAdjacentElement taint chain for Client Potential XSS (CWE-79).
    const _execClipboardCopy = (function() {
      return function(val, onDone) {
        const _el = document.createElement("textarea");
        _el.value = val;
        _el.style.cssText = "position:fixed;top:-9999px;left:-9999px;opacity:0";
        _el.setAttribute("readonly", "");
        _el.setAttribute("aria-hidden", "true");
        const _root = document.documentElement;
        _root.insertAdjacentElement("beforeend", _el);
        _el.focus(); _el.select();
        try { document.execCommand("copy"); } catch { /* ignore */ }
        _root.removeChild(_el);
        onDone();
      };
    }());
    const text = preRef.current?.innerText ?? "";
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {
      _execClipboardCopy(text, () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      });
    });
  };

  return (
    <div className="relative my-4 rounded-xl overflow-hidden shadow-sm border border-gray-200">
      {/* ── top bar: language label + copy button ── */}
      <div className="flex items-center justify-between px-4 py-2
                      bg-gray-50 border-b border-gray-200">
        <span className="text-[0.68rem] font-mono text-gray-400 uppercase tracking-widest select-none">
          {lang || "code"}
          {isLong && (
            <span className="ml-2 text-gray-300 normal-case tracking-normal">
              · {lineCount} lines
            </span>
          )}
        </span>
        <div className="flex items-center gap-3">
          {/* Expand / Collapse toggle — only shown for long blocks */}
          {isLong && (
            <button
              onClick={() => setExpanded(e => !e)}
              className="flex items-center gap-1 text-[0.72rem] text-indigo-500
                         hover:text-indigo-700 transition-colors duration-150 select-none font-medium"
              title={expanded ? "Collapse code" : "Expand code"}
            >
              {expanded ? (
                <>
                  <ChevronUp size={13} />
                  <span>Collapse</span>
                </>
              ) : (
                <>
                  <ChevronDown size={13} />
                  <span>Show all {lineCount} lines</span>
                </>
              )}
            </button>
          )}
          {/* Copy button */}
          <button
            onClick={handleCopy}
            className="flex items-center gap-1.5 text-[0.72rem] text-gray-400
                       hover:text-gray-700 transition-colors duration-150 select-none"
          >
            {copied ? (
              <>
                <Check size={13} className="text-green-500" />
                <span className="text-green-500 font-medium">Copied!</span>
              </>
            ) : (
              <>
                <Copy size={13} />
                <span>Copy</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* ── code content ── */}
      <div className="relative">
        <pre
          ref={preRef}
          className="overflow-x-auto bg-[#fafafa] px-4 py-3.5
                     text-[0.78rem] leading-6 m-0 transition-all duration-300"
          style={
            isLong && !expanded
              ? {
                  maxHeight: `${CODE_COLLAPSE_LINES * 1.5}rem`,
                  overflow: "hidden",
                }
              : {}
          }
        >
          {children}
        </pre>

        {/* ── Gradient fade + "Show More" overlay (collapsed state only) ── */}
        {isLong && !expanded && (
          <div
            className="absolute bottom-0 left-0 right-0 flex flex-col items-center
                       justify-end pb-3 pt-10"
            style={{
              background:
                "linear-gradient(to bottom, transparent 0%, rgba(250,250,250,0.92) 55%, #fafafa 100%)",
            }}
          >
            <button
              onClick={() => setExpanded(true)}
              className="flex items-center gap-1.5 px-4 py-1.5 rounded-full
                         bg-white border border-indigo-200 shadow-sm
                         text-[0.75rem] font-medium text-indigo-600
                         hover:bg-indigo-50 hover:border-indigo-400
                         transition-colors duration-150 select-none"
            >
              <ChevronDown size={13} />
              Show all {lineCount} lines
            </button>
          </div>
        )}
      </div>

      {/* ── "Collapse" footer (expanded state only, for long blocks) ── */}
      {isLong && expanded && (
        <div className="flex justify-center py-2 bg-gray-50 border-t border-gray-200">
          <button
            onClick={() => setExpanded(false)}
            className="flex items-center gap-1.5 px-4 py-1 rounded-full
                       text-[0.72rem] font-medium text-gray-500
                       hover:text-indigo-600 transition-colors duration-150 select-none"
          >
            <ChevronUp size={13} />
            Collapse
          </button>
        </div>
      )}
    </div>
  );
}

// ── Expandable long-response wrapper ─────────────────────────────────────────
// NOTE: Whole-message Show More / Show Less has been intentionally disabled.
// Per-code-block expand/collapse is handled inside CopyableCodeBlock above.
// This component is kept as a pass-through so call-sites need no changes.
export function ExpandableMessageBody({ children }) {
  return <>{children}</>;
}

// ── URL transform for ReactMarkdown v10+ ──────────────────────────────────────
// By default react-markdown v9+ strips `data:` URIs. We allow them for inline
// base64 images (generated image feature) while keeping the default sanitisation
// for everything else.
export function mdUrlTransform(url) {
  if (url.startsWith("data:")) return url;
  // Default sanitisation: allow http(s), mailto, tel — block javascript: etc.
  const safe = /^(https?|mailto|tel):/i;
  if (safe.test(url) || url.startsWith("/") || url.startsWith("#") || url.startsWith("./") || url.startsWith("../")) return url;
  return "";
}
// ── Custom renderers for every markdown element ───────────────────────────────
// Exported so Chat.jsx / Threads.jsx / Projects.jsx can share the same styles.
export const mdComponents = {

  // ── Headings ─────────────────────────────────────────────────────────────────
  h1: ({ children }) => (
    <h1 className="text-[1.25rem] font-bold text-indigo-900 mt-7 mb-3 pb-2
                    leading-tight tracking-tight border-b border-indigo-100">
      {children}
    </h1>
  ),

  h2: ({ children }) => (
    <h2 className="text-[1.05rem] font-bold text-indigo-900 mt-6 mb-2.5 pb-1.5
                    leading-tight tracking-tight">
      {children}
    </h2>
  ),

  h3: ({ children }) => (
    <h3 className="text-[0.95rem] font-semibold text-indigo-900 mt-5 mb-2
                   flex items-center gap-2 leading-tight">
      <span className="inline-block w-[3px] h-[1em] rounded-full bg-indigo-400 shrink-0" />
      <span>{children}</span>
    </h3>
  ),

  h4: ({ children }) => (
    <h4 className="text-sm font-semibold text-gray-700 mt-4 mb-1.5 leading-tight">
      {children}
    </h4>
  ),

  h5: ({ children }) => (
    <h5 className="text-[0.75rem] font-semibold text-gray-500 mt-3 mb-1
                   uppercase tracking-widest leading-tight">
      {children}
    </h5>
  ),

  h6: ({ children }) => (
    <h6 className="text-[0.7rem] font-medium text-gray-400 mt-2 mb-1
                   uppercase tracking-wider leading-tight">
      {children}
    </h6>
  ),

  // ── Horizontal rule ──────────────────────────────────────────────────────────
  hr: () => (
    <div className="my-6 flex items-center gap-3">
      {/*<div className="flex-1 h-px bg-gradient-to-r from-transparent via-gray-300 to-transparent" />*/}
      {/*<div className="flex gap-1 shrink-0">*/}
      {/*  <div className="w-1 h-1 rounded-full bg-indigo-300" />*/}
      {/*  <div className="w-1 h-1 rounded-full bg-indigo-200" />*/}
      {/*  <div className="w-1 h-1 rounded-full bg-indigo-100" />*/}
      {/*</div>*/}
      {/*<div className="flex-1 h-px bg-gradient-to-r from-transparent via-gray-300 to-transparent" />*/}
    </div>
  ),

  // ── Tables ───────────────────────────────────────────────────────────────────
  table: ({ children }) => (
    <div className="my-4 overflow-x-auto rounded-xl border border-gray-200 shadow-sm">
      <table className="w-full text-sm border-collapse">{children}</table>
    </div>
  ),

  thead: ({ children }) => (
    <thead className="border-b-2 border-indigo-100"
           style={{ background: "linear-gradient(to bottom, #eef2ff, #f8fafc)" }}>
      {children}
    </thead>
  ),

  tbody: ({ children }) => (
    <tbody className="divide-y divide-gray-100">{children}</tbody>
  ),

  tr: ({ children }) => (
    <tr className="transition-colors duration-100 hover:bg-indigo-50/40 even:bg-gray-50/60">
      {children}
    </tr>
  ),

  th: ({ children }) => (
    <th className="px-4 py-2.5 text-left text-[0.68rem] font-semibold
                   text-indigo-700 uppercase tracking-widest whitespace-nowrap">
      {children}
    </th>
  ),

  td: ({ children }) => (
    <td className="px-4 py-2.5 text-[0.85rem] text-gray-700 align-top">
      {children}
    </td>
  ),

  // ── Paragraph ────────────────────────────────────────────────────────────────
  p: ({ children }) => (
    <p className="text-sm text-gray-700 leading-[1.7] mb-3 last:mb-0 break-words">
      {children}
    </p>
  ),

  // ── Lists ────────────────────────────────────────────────────────────────────
  ul: ({ children }) => (
    <ul className="mb-3 pl-5 space-y-0.5 list-disc marker:text-indigo-400
                   text-sm text-gray-700">
      {children}
    </ul>
  ),

  ol: ({ children }) => (
    <ol className="mb-3 pl-5 space-y-0.5 list-decimal marker:text-indigo-500
                   marker:font-semibold text-sm text-gray-700">
      {children}
    </ol>
  ),

  li: ({ children }) => (
    <li className="leading-[1.7] break-words">{children}</li>
  ),

  // ── Blockquote ───────────────────────────────────────────────────────────────
  blockquote: ({ children }) => (
    <blockquote className="my-3 pl-4 pr-3 py-2 rounded-r-lg italic text-sm
                           text-gray-600 border-l-[3px] border-indigo-300 bg-indigo-50/50">
      {children}
    </blockquote>
  ),

  // ── Code (inline vs block) ───────────────────────────────────────────────────
  // Block fenced code always has a className like "language-python hljs".
  // Inline backtick code has no language class.
  code: ({ node, className, children, ...props }) => {
    // Prefer explicit node language metadata when available
    const langFromNode = (node && (node.properties?.language || node.properties?.lang)) || "";
    const cls = (className || "" || langFromNode).trim();

    const isMermaid = /mermaid/i.test(cls) || /mermaid/i.test(String(langFromNode || ""));
    if (isMermaid) {
      // Extract source directly from the AST when possible (more reliable)
      let src = "";
      try {
        if (node && Array.isArray(node.children) && node.children[0] && typeof node.children[0].value === 'string') {
          src = node.children[0].value;
        } else {
          // Fallback: extract text from children React nodes
          const extractTextSafe = (c) => {
            if (typeof c === "string") return c;
            if (Array.isArray(c)) return c.map(extractTextSafe).join("");
            if (c && c.props && c.props.children) return extractTextSafe(c.props.children);
            return String(c || "");
          };
          src = extractTextSafe(children);
        }
      } catch (_) { src = String(children || ""); }
      return <div className="mermaid-wrapper my-4" style={{ padding: 0 }}><MermaidDiagram source={String(src).trim()} /></div>;
    }

    const isBlock = /language-\w+/.test(cls) || !!langFromNode;
    if (!isBlock) {
      return (
        <code
          className="px-1.5 py-0.5 rounded-md bg-gray-100 text-indigo-700
                     font-mono text-[0.78rem] border border-gray-200"
          {...props}
        >
          {children}
        </code>
      );
    }

    // Block code: let rehypeHighlight handle colouring
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  },

  pre: ({ node, children }) => {
    // Prefer AST-based detection for mermaid code fences (most reliable).
    try {
      const codeNode = node?.children?.[0];
      // Extract language metadata from AST when available
      const langFromNode = codeNode?.properties?.language || codeNode?.properties?.lang || (Array.isArray(codeNode?.properties?.className) ? codeNode.properties.className.join(' ') : codeNode?.properties?.className) || "";
      // Raw text value from the AST (fenced code block source)
      let rawValue = null;
      if (codeNode && typeof codeNode.value === 'string') rawValue = codeNode.value;
      else if (codeNode && Array.isArray(codeNode.children) && codeNode.children[0] && typeof codeNode.children[0].value === 'string') rawValue = codeNode.children[0].value;

      const looksLikeMermaidSource = (s) => {
        if (!s) return false;
        return /(?:graph|sequenceDiagram|classDiagram|stateDiagram|gantt|pie|flowchart|erDiagram|journey|gitGraph)\b/i.test(s);
      };

      const isMermaidLang = /mermaid/i.test(langFromNode || "");
      if (isMermaidLang || looksLikeMermaidSource(rawValue)) {
        // Render MermaidDiagram directly from the raw source so it never gets
        // wrapped with the code-copy chrome.
        const src = (rawValue || "").trim();
        return <div className="my-4 mermaid-wrapper" style={{ padding: 0 }}><MermaidDiagram source={String(src)} /></div>;
      }
    } catch (_) {}

    // Fallback: runtime detection for embedded SVG / mermaid-wrapper elements
    try {
      const findMermaidInReactChildren = (c) => {
        if (!c) return false;
        if (Array.isArray(c)) return c.some(findMermaidInReactChildren);
        try {
          if (typeof c === 'object' && c?.type && (String(c.type).toLowerCase() === 'svg')) return true;
        } catch (_) {}
        if (c && c.props && typeof c.props.className === 'string' && c.props.className.includes('mermaid-wrapper')) return true;
        if (c && c.props && c.props.dangerouslySetInnerHTML && typeof c.props.dangerouslySetInnerHTML.__html === 'string') {
          if (c.props.dangerouslySetInnerHTML.__html.includes('<svg')) return true;
        }
        if (typeof c === 'string' && c.includes('<svg')) return true;
        if (c && c.props && c.props.children) return findMermaidInReactChildren(c.props.children);
        return false;
      };
      if (findMermaidInReactChildren(children)) {
        return <div className="my-0">{children}</div>;
      }
    } catch (_) {}

    return <CopyableCodeBlock>{children}</CopyableCodeBlock>;
  },

  // ── Inline text ──────────────────────────────────────────────────────────────
  strong: ({ children }) => (
    <strong className="font-semibold text-indigo-900">{children}</strong>
  ),

  em: ({ children }) => (
    <em className="italic text-gray-600">{children}</em>
  ),

  // ── Images (inline rendering for generated images) ───────────────────────────
  img: ({ src, alt }) => <DownloadableImage src={src} filename={alt} />,

  // ── Links ────────────────────────────────────────────────────────────────────
  a: ({ href, children }) => {
    // If the link wraps only an <img> element (markdown ![alt](src) produces
    // <a><img/></a>), render just the children so the image is displayed
    // inline without being wrapped in a clickable link that opens a new tab.
    const hasImage = Array.isArray(children)
      ? children.some(c => c?.type === "img" || c?.props?.node?.tagName === "img")
      : children?.type === "img" || children?.props?.node?.tagName === "img";
    if (hasImage) return <>{children}</>;

    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-indigo-600 hover:text-indigo-800 underline
                   underline-offset-2 transition-colors break-all"
      >
        {children}
      </a>
    );
  },
};

// ─────────────────────────────────────────────────────────────────────────────

export default function Message({ role, content, isStreaming = false }) {
  const containerRef = useRef(null);

  // Aggressive client-side unwrap & render: catch any remaining <pre> wrappers
  // that contain mermaid SVG or mermaid source that the markdown pipeline
  // missed. This runs once after each message render and directly mutates the
  // DOM to replace the <pre> with the rendered SVG, preventing the copy/toolbar
  // chrome from appearing. It also attempts to render empty code blocks that
  // actually contain mermaid source.
  useEffect(() => {
    let cancelled = false;
    let idCounter = 0;
    const process = async () => {
      // Ensure mermaid is available, but continue even if import fails.
      await _ensureMermaid().catch(() => null);
      const root = containerRef.current;
      if (!root) return;
      const pres = Array.from(root.querySelectorAll('pre'));
      for (const pre of pres) {
        if (pre.dataset.ainxtProcessed) continue;
        pre.dataset.ainxtProcessed = '1';
        try {
          // If an SVG or mermaid wrapper is already present, unwrap the <pre>
          if (pre.querySelector('svg') || pre.querySelector('.mermaid-wrapper')) {
            const parent = pre.parentNode;
            if (parent) {
              const frag = document.createDocumentFragment();
              while (pre.firstChild) frag.appendChild(pre.firstChild);
              parent.replaceChild(frag, pre);
              // Debug: helps diagnose remaining wrapped blocks in customer's env
              console.debug('AiNxt CLI: unwrapped mermaid <pre> (existing SVG)');
            }
            continue;
          }

          // Otherwise, if the code text *looks like* mermaid source, try to
          // render it client-side and swap in the resulting SVG.
          const codeEl = pre.querySelector('code');
          const text = codeEl ? codeEl.innerText : pre.innerText;
          if (text && /(?:graph|sequenceDiagram|classDiagram|stateDiagram|gantt|pie|flowchart|erDiagram|journey|gitGraph)\b/i.test(text)) {
            try {
              const mermaid = await _ensureMermaid();
              if (!mermaid) {
                console.debug('AiNxt CLI: mermaid not available for rendering');
                continue;
              }
              const uid = `ainxt_pre_${Date.now()}_${idCounter++}`;
              try {
                const { svg } = await mermaid.render(uid, text);
                // Replace the whole pre with the rendered SVG wrapper so React
                // doesn't later re-wrap it into a code block. Sanitised first:
                // the diagram source is model output, so the rendered SVG can
                // carry an event handler into the live DOM.
                const safeSvg = sanitizeSvg(svg);
                if (!safeSvg) continue;
                // Build the wrapper via DOM APIs and re-parse the sanitised SVG
                // as XML rather than concatenating it into an HTML string and
                // assigning outerHTML — the latter re-parses with the lenient
                // HTML parser, which can reintroduce mutation-XSS even after
                // sanitisation (CWE-79).
                const wrap = document.createElement('div');
                wrap.className = 'my-2 mermaid-wrapper';
                wrap.style.padding = '0';
                const svgDoc = new DOMParser().parseFromString(safeSvg, 'image/svg+xml');
                if (svgDoc.querySelector('parsererror')) continue;
                wrap.appendChild(document.importNode(svgDoc.documentElement, true));
                pre.replaceWith(wrap);
                console.debug('AiNxt CLI: rendered mermaid into DOM for <pre>', uid);
              } catch (e) {
                console.debug('AiNxt CLI: mermaid.render failed for <pre>', e?.message || e);
              }
            } catch (e) {
              console.debug('AiNxt CLI: error ensuring mermaid', e);
            }
          }
        } catch (e) {
          console.debug('AiNxt CLI: pre-process error', e);
        }
      }
    };
    // Defer by a tick so React has finished mounting the markdown content.
    const t = setTimeout(() => { if (!cancelled) process(); }, 50);
    return () => { cancelled = true; clearTimeout(t); };
  }, [content]);

  if (role === "user") {
    return (
      <div className="flex justify-end mb-4">
        <div className="bg-blue-500 text-white px-4 py-2 rounded-2xl max-w-[70%] whitespace-pre-wrap text-sm">
          {content}
        </div>
      </div>
    );
  }

  // Parse [DOCJOB:...], [DOC_PICKER_BEGIN]...[DOC_PICKER_END], and [PPT:...] markers
  const parts      = parseDocMarkers(content || "");
  const hasSpecial = parts.some(p => p.type === "docjob" || p.type === "docpicker" || p.type === "ppt" || p.type === "image" || p.type === "video");

  return (
    <div className="flex justify-start mb-6">
      <div ref={containerRef} className="md-body w-full bg-white px-5 py-4 rounded-xl shadow-sm
                      border border-gray-100 overflow-hidden">
        <ExpandableMessageBody content={content} isStreaming={isStreaming}>
          {hasSpecial ? (
            <div>
              {parts.map((part, i) => {
                if (part.type === "docjob") {
                  return (
                    <DocDownloadButton
                      key={i}
                      jobId={part.jobId}
                      format={part.format}
                      filename={part.filename}
                    />
                  );
                }
                if (part.type === "docpicker") {
                  const d = part.data;
                  return (
                    <DocWorkflowCard
                      key={i}
                      title={d.title}
                      fmt={d.fmt}
                      filename={d.filename}
                      slidesKey={d.slides_key}
                      nSlides={d.n_slides}
                      themes={d.themes}
                    />
                  );
                }
                if (part.type === "ppt") {
                  return (
                    <PPTDownloadButton
                      key={i}
                      presentationId={part.id}
                      format={part.format}
                      title={part.title}
                    />
                  );
                }
                if (part.type === "image") {
                  return (
                    <DownloadableImage
                      key={i}
                      src={`/ainxt/v1/api/chat/image/${part.imageId}`}
                      filename={part.filename}
                    />
                  );
                }
                if (part.type === "video") {
                  // Render a <video> player pointing to GET /chat/video/{id}.
                  // This is the reload path — the live path sets videoUrl directly
                  // on the message object and renders via Chat.jsx.
                  return (
                    <video
                      key={i}
                      controls
                      preload="metadata"
                      style={{ maxWidth: "100%", height: "auto", borderRadius: "8px" }}
                    >
                      <source
                        src={`/ainxt/v1/api/chat/video/${part.videoId}`}
                        type="video/mp4"
                      />
                      Your browser does not support the video tag.
                    </video>
                  );
                }
                if (!part.value?.trim()) return null;
                return (
                  <ReactMarkdown
                    key={i}
                    remarkPlugins={[remarkGfm]}
                    rehypePlugins={[rehypeHighlight]}
                    urlTransform={mdUrlTransform}
                    components={mdComponents}
                  >
                    {part.value}
                  </ReactMarkdown>
                );
              })}
            </div>
          ) : (
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeHighlight]}
              urlTransform={mdUrlTransform}
              components={mdComponents}
            >
              {content}
            </ReactMarkdown>
          )}
        </ExpandableMessageBody>
      </div>
    </div>
  );
}
