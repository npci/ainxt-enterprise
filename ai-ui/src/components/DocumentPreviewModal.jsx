// SPDX-License-Identifier: MIT
/**
 * DocumentPreviewModal — renders uploaded file content in a modal overlay.
 *
 * File bytes are fetched via the Cache API (browser-side cache, 7-day TTL).
 * First preview hits the server; subsequent previews load instantly from cache.
 *
 * Rendering strategy by file type:
 *   - PDF           → <iframe> with browser's native PDF viewer
 *   - Images        → <img> tag
 *   - Text/CSV/JSON/XML → <pre> with fetched plain text
 *   - HTML          → sandboxed <iframe>
 *   - DOCX          → pure-JS extractor (ZIP + XML → simplified HTML)
 *   - XLSX/XLS      → pure-JS parser (ZIP + XML → HTML table)
 *   - Markdown      → react-markdown (already installed)
 *   - PPTX          → pure-JS extractor (ZIP + XML → per-slide outline)
 *   - PPT/RTF       → parsed text + info banner
 *   - Unknown       → warning message
 *
 * Props:
 *   attachmentId  — UUID of the attachment (used for /file endpoint)
 *   fileName      — display name for the header
 *   fileType      — extension string (e.g. "pdf", "docx")
 *   parsedText    — full parsed text content (from upload response)
 *   onClose       — callback to close the modal
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { X, Download, FileText, Loader2, AlertCircle, Info, AlertTriangle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cachedGet, cachedGetOrFetch } from "../utils/previewCache";

// ── File type → rendering strategy ──────────────────────────────────────────
const STRATEGY = {
  pdf:  "iframe",
  png:  "image",  jpg:  "image",  jpeg: "image",
  gif:  "image",  webp: "image",  bmp:  "image",
  txt:  "text",   csv:  "text",   json: "text",   xml:  "text",
  html: "html",   htm:  "html",
  docx: "docx",
  xlsx: "xlsx",   xls:  "xlsx",
  md:   "markdown",
  pptx: "pptx",
  ppt:  "parsed", rtf:  "parsed",
};

const PARSED_TYPES = new Set(["ppt", "rtf"]);

function getStrategy(fileType) {
  return STRATEGY[fileType?.toLowerCase()] || "unsupported";
}

// Exported for testing
export { STRATEGY, PARSED_TYPES, getStrategy };

// ── Hook: read file from browser Cache API (no server calls) ────────────────

function useCachedFile(attachmentId, responseType = "text") {
  const [data, setData]       = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);

    (async () => {
      try {
        const res = await cachedGetOrFetch(attachmentId);
        if (!res) throw new Error("File not found. It may have expired — please re-upload.");

        let result;
        if (responseType === "blob")            result = await res.blob();
        else if (responseType === "arraybuffer") result = await res.arrayBuffer();
        else                                     result = await res.text();

        if (!cancelled) setData(result);
      } catch (e) {
        if (!cancelled) setError(e.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [attachmentId, responseType]);

  return { data, loading, error };
}

// ── Sub-renderers ───────────────────────────────────────────────────────────

function IframeRenderer({ attachmentId, sandbox }) {
  const [blobUrl, setBlobUrl] = useState(null);
  const [loaded, setLoaded]   = useState(false);
  const { data, loading, error } = useCachedFile(attachmentId, "blob");

  useEffect(() => {
    if (!data) return;
    const url = URL.createObjectURL(data);
    setBlobUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [data]);

  if (loading) return <LoadingSpinner />;
  if (error)   return <ErrorMessage message={error} />;
  if (!blobUrl) return <LoadingSpinner />;

  return (
    <>
      {!loaded && <LoadingSpinner />}
      <iframe
        src={blobUrl}
        title="Document preview"
        className={`w-full h-full border-0 rounded-b-lg ${loaded ? "" : "hidden"}`}
        sandbox={sandbox ? "allow-same-origin" : undefined}
        onLoad={() => setLoaded(true)}
      />
    </>
  );
}

function ImageRenderer({ attachmentId, alt }) {
  const [blobUrl, setBlobUrl] = useState(null);
  const [loaded, setLoaded]   = useState(false);
  const [imgError, setImgError] = useState(false);
  const { data, loading, error } = useCachedFile(attachmentId, "blob");

  useEffect(() => {
    if (!data) return;
    const url = URL.createObjectURL(data);
    setBlobUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [data]);

  if (loading) return <LoadingSpinner />;
  if (error)   return <ErrorMessage message={error} />;

  return (
    <div className="flex items-center justify-center h-full p-4 overflow-auto">
      {!loaded && !imgError && blobUrl && <LoadingSpinner />}
      {imgError && <ErrorMessage message="Failed to load image." />}
      {blobUrl && (
        <img
          src={blobUrl}
          alt={alt}
          className={`max-w-full max-h-full object-contain rounded ${loaded ? "" : "hidden"}`}
          onLoad={() => setLoaded(true)}
          onError={() => setImgError(true)}
        />
      )}
    </div>
  );
}

function TextRenderer({ attachmentId }) {
  const { data, loading, error } = useCachedFile(attachmentId, "text");

  if (loading) return <LoadingSpinner />;
  if (error)   return <ErrorMessage message={error} />;

  return (
    <pre className="whitespace-pre-wrap break-words text-sm text-gray-700 leading-relaxed p-4 overflow-auto h-full font-mono">
      {data}
    </pre>
  );
}

// ── DOCX renderer (pure-JS: ZIP + XML → HTML) ──────────────────────────────

function DocxRenderer({ attachmentId }) {
  const [html, setHtml]                   = useState("");
  const [renderLoading, setRenderLoading] = useState(true);
  const [renderError, setRenderError]     = useState(null);
  const { data: arrayBuf, loading, error } = useCachedFile(attachmentId, "arraybuffer");

  useEffect(() => {
    if (!arrayBuf) return;
    let cancelled = false;

    (async () => {
      try {
        const { extractDocxHtml } = await import("../utils/docxTextExtractor.js");
        if (cancelled) return;
        const result = await extractDocxHtml(arrayBuf);
        if (!cancelled) setHtml(result);
      } catch (e) {
        if (!cancelled) setRenderError(e.message);
      } finally {
        if (!cancelled) setRenderLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [arrayBuf]);

  if (error || renderError) return <ErrorMessage message={error || renderError} />;

  return (
    <div className="h-full overflow-auto bg-gray-50 relative flex flex-col">
      {(loading || renderLoading) && (
        <div className="absolute inset-0 flex items-center justify-center z-10 bg-gray-50/80">
          <LoadingSpinner />
        </div>
      )}
      <div className="flex items-center gap-2 px-5 py-2.5 bg-blue-50 border-b border-blue-100 text-xs text-blue-700 flex-shrink-0">
        <Info size={14} className="flex-shrink-0" />
        <span>Simplified preview — formatting may differ. Download for full fidelity.</span>
      </div>
      <div
        className="flex-1 overflow-auto docx-container"
        dangerouslySetInnerHTML={{ __html: html }}
      />
      <style>{`
        .docx-container {
          padding: 24px 32px;
          max-width: 800px;
          margin: 0 auto;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          font-size: 14px;
          line-height: 1.7;
          color: #1f2937;
        }
        .docx-container h1 { font-size: 1.75em; font-weight: 700; margin: 1em 0 0.4em; color: #111827; }
        .docx-container h2 { font-size: 1.4em;  font-weight: 600; margin: 0.9em 0 0.35em; color: #1f2937; }
        .docx-container h3 { font-size: 1.15em; font-weight: 600; margin: 0.8em 0 0.3em; color: #374151; }
        .docx-container h4, .docx-container h5, .docx-container h6 {
          font-size: 1em; font-weight: 600; margin: 0.7em 0 0.25em; color: #374151;
        }
        .docx-container p  { margin: 0.35em 0; }
        .docx-container ul { margin: 0.3em 0 0.3em 1.5em; list-style: disc; }
        .docx-container li { margin: 0.15em 0; }
        .docx-container table {
          border-collapse: collapse; width: 100%; margin: 0.8em 0; font-size: 13px;
        }
        .docx-container td {
          border: 1px solid #d1d5db; padding: 6px 10px; vertical-align: top;
        }
        .docx-container tr:nth-child(even) { background: #f9fafb; }
      `}</style>
    </div>
  );
}

// ── PPTX renderer (pure-JS: ZIP + XML → per-slide outline) ─────────────────

function PptxRenderer({ attachmentId }) {
  const [html, setHtml]                   = useState("");
  const [renderLoading, setRenderLoading] = useState(true);
  const [renderError, setRenderError]     = useState(null);
  const { data: arrayBuf, loading, error } = useCachedFile(attachmentId, "arraybuffer");

  useEffect(() => {
    if (!arrayBuf) return;
    let cancelled = false;

    (async () => {
      try {
        const { extractPptxHtml } = await import("../utils/pptxTextExtractor.js");
        if (cancelled) return;
        const result = await extractPptxHtml(arrayBuf);
        if (!cancelled) setHtml(result);
      } catch (e) {
        if (!cancelled) setRenderError(e.message);
      } finally {
        if (!cancelled) setRenderLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [arrayBuf]);

  if (error || renderError) return <ErrorMessage message={error || renderError} />;

  return (
    <div className="h-full overflow-auto bg-gray-50 relative flex flex-col">
      {(loading || renderLoading) && (
        <div className="absolute inset-0 flex items-center justify-center z-10 bg-gray-50/80">
          <LoadingSpinner />
        </div>
      )}
      <div className="flex items-center gap-2 px-5 py-2.5 bg-blue-50 border-b border-blue-100 text-xs text-blue-700 flex-shrink-0">
        <Info size={14} className="flex-shrink-0" />
        <span>Text outline preview — slide visuals and images are not shown. Download for full fidelity.</span>
      </div>
      <div
        className="flex-1 overflow-auto pptx-container"
        dangerouslySetInnerHTML={{ __html: html }}
      />
      <style>{`
        .pptx-container {
          padding: 20px 24px;
          max-width: 820px;
          margin: 0 auto;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          font-size: 14px;
          color: #1f2937;
        }
        .pptx-container .pptx-slide {
          background: #fff;
          border: 1px solid #e5e7eb;
          border-radius: 8px;
          padding: 16px 20px;
          margin: 0 0 16px;
          box-shadow: 0 1px 2px rgba(0,0,0,0.04);
        }
        .pptx-container .pptx-slide-num {
          font-size: 11px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: #6366f1;
          margin-bottom: 8px;
        }
        .pptx-container ul { margin: 0 0 0 1.25em; list-style: disc; }
        .pptx-container li { margin: 0.2em 0; line-height: 1.6; }
        .pptx-container .pptx-empty { color: #9ca3af; font-style: italic; margin: 0; }
      `}</style>
    </div>
  );
}

// ── XLSX renderer (pure-JS: ZIP + XML → HTML table) ────────────────────────

/**
 * Convert a parsed sheet's rows[][] into an HTML table string.
 */
function sheetToHtml(rows) {
  if (!rows || !rows.length) return "<p>Empty sheet</p>";

  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines = ["<table>"];

  for (let r = 0; r < rows.length; r++) {
    lines.push("<tr>");
    const tag = r === 0 ? "th" : "td";
    for (let c = 0; c < rows[r].length; c++) {
      lines.push(`<${tag}>${esc(rows[r][c])}</${tag}>`);
    }
    lines.push("</tr>");
  }

  lines.push("</table>");
  return lines.join("");
}

function XlsxRenderer({ attachmentId }) {
  const [html, setHtml]       = useState("");
  const [sheets, setSheets]   = useState([]);   // [{ name, rows }]
  const [activeSheet, setActiveSheet] = useState(0);
  const [renderError, setRenderError] = useState(null);
  const parsedRef = useRef(null);  // holds parsed { sheets } for tab switching
  const { data: arrayBuf, loading, error } = useCachedFile(attachmentId, "arraybuffer");

  useEffect(() => {
    if (!arrayBuf) return;
    let cancelled = false;

    (async () => {
      try {
        const { parseXlsx } = await import("../utils/xlsxParser.js");
        if (cancelled) return;

        const result = await parseXlsx(arrayBuf);
        parsedRef.current = result;
        setSheets(result.sheets);
        setActiveSheet(0);
        setHtml(sheetToHtml(result.sheets[0]?.rows));
      } catch (e) {
        if (!cancelled) setRenderError(e.message);
      }
    })();

    return () => { cancelled = true; };
  }, [arrayBuf]);

  const switchSheet = useCallback((idx) => {
    const parsed = parsedRef.current;
    if (!parsed) return;
    setHtml(sheetToHtml(parsed.sheets[idx]?.rows));
    setActiveSheet(idx);
  }, []);

  if (loading)                return <LoadingSpinner />;
  if (error || renderError)   return <ErrorMessage message={error || renderError} />;

  return (
    <div className="flex flex-col h-full">
      {sheets.length > 1 && (
        <div className="flex items-center gap-0.5 px-3 py-1.5 border-b border-gray-200 bg-gray-50 flex-shrink-0 overflow-x-auto">
          {sheets.map((sheet, i) => (
            <button
              key={sheet.name}
              onClick={() => switchSheet(i)}
              className={`px-3 py-1 text-xs rounded-t transition cursor-pointer ${
                i === activeSheet
                  ? "bg-white text-indigo-700 font-medium border border-b-0 border-gray-200"
                  : "text-gray-500 hover:text-gray-700 hover:bg-gray-100"
              }`}
            >
              {sheet.name}
            </button>
          ))}
        </div>
      )}
      <div
        className="flex-1 overflow-auto p-4 xlsx-preview"
        dangerouslySetInnerHTML={{ __html: html }}
      />
      <style>{`
        .xlsx-preview table { border-collapse: collapse; width: 100%; font-size: 13px; }
        .xlsx-preview td, .xlsx-preview th {
          border: 1px solid #e5e7eb; padding: 4px 8px; text-align: left;
          white-space: nowrap; max-width: 300px; overflow: hidden; text-overflow: ellipsis;
        }
        .xlsx-preview th { background: #f9fafb; font-weight: 600; color: #374151; }
        .xlsx-preview tr:nth-child(even) { background: #f9fafb; }
        .xlsx-preview tr:hover { background: #eef2ff; }
      `}</style>
    </div>
  );
}

// ── Markdown renderer ───────────────────────────────────────────────────────

function MarkdownRenderer({ attachmentId }) {
  const { data, loading, error } = useCachedFile(attachmentId, "text");

  if (loading) return <LoadingSpinner />;
  if (error)   return <ErrorMessage message={error} />;

  return (
    <div className="overflow-auto h-full px-6 py-4 prose prose-sm max-w-none text-gray-700">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {data}
      </ReactMarkdown>
    </div>
  );
}

// ── Parsed text viewer (PPT/RTF fallback) ───────────────────────────────────

function ParsedTextViewer({ text }) {
  if (!text) return <EmptyMessage />;
  return (
    <pre className="whitespace-pre-wrap break-words text-sm text-gray-700 leading-relaxed p-4 overflow-auto h-full font-mono">
      {text}
    </pre>
  );
}

// ── Unsupported file warning ────────────────────────────────────────────────

function UnsupportedMessage({ fileType }) {
  const ext = (fileType || "").toUpperCase();
  return (
    <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center">
      <AlertTriangle size={36} className="text-amber-400" />
      <p className="text-sm font-medium text-gray-700">
        Preview not available for <span className="font-semibold">.{ext}</span> files
      </p>
      <p className="text-xs text-gray-400 max-w-sm">
        Your browser does not support rendering this file type.
        Use the download button to open it in a compatible application.
      </p>
    </div>
  );
}

// ── Shared UI atoms ─────────────────────────────────────────────────────────

function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center h-full">
      <Loader2 size={28} className="animate-spin text-indigo-400" />
    </div>
  );
}

function ErrorMessage({ message }) {
  const isExpired = message?.toLowerCase().includes("cache") ||
                    message?.toLowerCase().includes("expired") ||
                    message?.toLowerCase().includes("not found in browser");
  if (isExpired) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center">
        <AlertCircle size={36} className="text-amber-400" />
        <p className="text-sm font-medium text-gray-700">Preview no longer available</p>
        <p className="text-xs text-gray-400 max-w-sm">
          This file's preview has expired from your browser cache (files are kept for 7 days).
        </p>
      </div>
    );
  }
  return (
    <div className="flex items-center justify-center h-full">
      <div className="flex items-center gap-2 text-sm text-red-500">
        <AlertCircle size={16} />
        <span>{message}</span>
      </div>
    </div>
  );
}

function EmptyMessage() {
  return (
    <div className="flex items-center justify-center h-full text-sm text-gray-400">
      No content available for preview.
    </div>
  );
}

// ── Main modal component ────────────────────────────────────────────────────

export default function DocumentPreviewModal({ attachmentId, fileName, fileType, parsedText, onClose }) {
  const strategy  = getStrategy(fileType);
  const isParsed  = PARSED_TYPES.has(fileType?.toLowerCase());
  const ext       = (fileType || "").toUpperCase();

  // Close on Escape key
  useEffect(() => {
    const handleKey = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  // Download handler — cache-first, then authenticated server fallback.
  const handleDownload = useCallback(async () => {
    try {
      const res = await cachedGetOrFetch(attachmentId);
      if (!res) throw new Error("Not available");
      const blob = await res.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = fileName || "file";
      a.click();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    } catch {
      // Nothing to fall back to — file only exists in browser cache
    }
  }, [attachmentId, fileName]);

  // Render the appropriate content viewer
  function renderContent() {
    switch (strategy) {
      case "iframe":
        return <IframeRenderer attachmentId={attachmentId} />;
      case "image":
        return <ImageRenderer attachmentId={attachmentId} alt={fileName} />;
      case "text":
        return <TextRenderer attachmentId={attachmentId} />;
      case "html":
        return <IframeRenderer attachmentId={attachmentId} sandbox />;
      case "docx":
        return <DocxRenderer attachmentId={attachmentId} />;
      case "pptx":
        return <PptxRenderer attachmentId={attachmentId} />;
      case "xlsx":
        return <XlsxRenderer attachmentId={attachmentId} />;
      case "markdown":
        return <MarkdownRenderer attachmentId={attachmentId} />;
      case "parsed":
        return <ParsedTextViewer text={parsedText} />;
      case "unsupported":
      default:
        return <UnsupportedMessage fileType={fileType} />;
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-lg shadow-2xl flex flex-col overflow-hidden"
        style={{ width: "96vw", height: "94vh" }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-100 flex-shrink-0">
          <div className="flex items-center gap-2.5 min-w-0">
            <FileText size={16} className="text-indigo-500 flex-shrink-0" />
            <span className="text-sm font-medium text-gray-800 truncate">{fileName}</span>
            {ext && (
              <span className="text-[10px] font-semibold text-indigo-600 bg-indigo-50 px-1.5 py-0.5 rounded flex-shrink-0 uppercase">
                {ext}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1 flex-shrink-0">
            <button
              onClick={handleDownload}
              title="Download file"
              className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition cursor-pointer"
            >
              <Download size={16} />
            </button>
            <button
              onClick={onClose}
              title="Close preview"
              className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition cursor-pointer"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Parsed-text-only info banner (PPT, RTF) */}
        {isParsed && (
          <div className="flex items-center gap-2 px-5 py-2.5 bg-blue-50 border-b border-blue-100 text-xs text-blue-700">
            <Info size={14} className="flex-shrink-0" />
            <span>No browser viewer available for this file type. Showing parsed content instead.</span>
          </div>
        )}

        {/* Content area */}
        <div className="flex-1 min-h-0 overflow-hidden">
          {renderContent()}
        </div>
      </div>
    </div>
  );
}
