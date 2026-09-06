// SPDX-License-Identifier: MIT
import { useState, useEffect } from "react";
import { Loader2, FileDown, Presentation, Check } from "lucide-react";
import { authFetch } from "../config";
import { validateIdentifier } from "../utils/securityValidation";
import DocPreviewCard from "./DocPreviewCard";

// Visual metadata per theme (must match PPTX_THEMES in doc_generator.py)
const THEME_META = {
  dark_executive: {
    gradient: "linear-gradient(135deg, #060D1A 0%, #003366 100%)",
    accent:   "#FF6600",
    textHigh: "#FFFFFF",
    textLow:  "#99BBDD",
    badge:    "bg-orange-500",
  },
  light_modern: {
    gradient: "linear-gradient(135deg, #1A2744 0%, #F0F4FF 100%)",
    accent:   "#1A73E8",
    textHigh: "#1A2744",
    textLow:  "#555577",
    badge:    "bg-blue-500",
  },
  vibrant_tech: {
    gradient: "linear-gradient(120deg, #050F1E 0%, #003355 100%)",
    accent:   "#00AACC",
    textHigh: "#FFFFFF",
    textLow:  "#99CCDD",
    badge:    "bg-cyan-500",
  },
};

// ── Reusable polling download button (mirrors DocDownloadButton in Message.jsx) ──
function InlineDownloadButton({ jobId, filename, format = "pptx" }) {
  const [status, setStatus] = useState("polling");
  const [fileId, setFileId] = useState(null);
  const [errMsg, setErrMsg] = useState("");
  const [summary, setSummary] = useState(null);
  const [preview, setPreview] = useState(null);

  useEffect(() => {
    const iv = setInterval(async () => {
      try {
        const r    = await authFetch(`/ainxt/v1/api/docs/job/${jobId}/status`);
        const data = await r.json();
        if (data.status === "done") {
          setFileId(data.file_id);
          if (Array.isArray(data.summary)) setSummary(data.summary);
          if (data.preview && typeof data.preview === "object") setPreview(data.preview);
          setStatus("ready");
          clearInterval(iv);
        } else if (data.status === "error") {
          setErrMsg(data.error || "Generation failed");
          setStatus("error");
          clearInterval(iv);
        }
      } catch (_) {}
    }, 2000);
    return () => clearInterval(iv);
  }, [jobId]);

  const download = () => {
    authFetch(`/ainxt/v1/api/docs/download/${fileId}`)
      .then(r => r.blob())
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const a   = document.createElement("a");
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click();
        document.body.removeChild(a);
        // Defer revocation: revoking synchronously right after click() can abort
        // the download before the browser reads the blob (slow browsers / large
        // files → empty/failed download). Same 1s delay as DocumentPreviewModal.
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      })
      .catch(() => {});
  };

  if (status === "polling") {
    return (
      <div className="flex items-center gap-1.5 text-xs text-indigo-600 mt-2">
        <Loader2 size={12} className="animate-spin" />
        <span>Generating…</span>
      </div>
    );
  }
  if (status === "error") {
    return <p className="text-xs text-red-500 mt-2">{errMsg}</p>;
  }
  return (
    <>
      <button
        onClick={download}
        className="flex items-center gap-1.5 mt-2 text-xs px-3 py-1.5
                   rounded-lg bg-green-600 hover:bg-green-700 text-white font-medium
                   transition-colors shadow-sm"
      >
        <FileDown size={12} /> Download
      </button>
      {(summary || preview) && (
        <DocPreviewCard summary={summary} preview={preview} />
      )}
    </>
  );
}

// ── Theme card ────────────────────────────────────────────────────────────────
function ThemeCard({ theme, slidesKey, title, fmt, filename, onGenerate, job }) {
  const meta = THEME_META[theme.id] || THEME_META.dark_executive;

  return (
    <div className="flex flex-col rounded-xl border border-gray-200 overflow-hidden
                    shadow-sm hover:shadow-md transition-shadow w-52 shrink-0">
      {/* Gradient preview strip */}
      <div
        className="h-24 w-full relative flex flex-col items-center justify-center px-3"
        style={{ background: meta.gradient }}
      >
        {/* Simulated slide layout */}
        <div className="w-full space-y-1.5">
          <div className="h-2 rounded-full opacity-80 w-4/5 mx-auto"
               style={{ background: meta.textHigh }} />
          <div className="h-1 rounded-full opacity-40 w-3/5 mx-auto"
               style={{ background: meta.textHigh }} />
        </div>
        {/* Bottom accent bar */}
        <div className="absolute bottom-0 left-0 right-0 h-1.5"
             style={{ background: meta.accent }} />
        {/* Already-generated checkmark */}
        {job && (
          <div className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full bg-green-500
                          flex items-center justify-center">
            <Check size={11} strokeWidth={3} className="text-white" />
          </div>
        )}
      </div>

      {/* Card body */}
      <div className="p-3 bg-white flex flex-col gap-1 flex-1">
        <p className="text-sm font-semibold text-gray-800">{theme.name}</p>
        <p className="text-xs text-gray-500 leading-snug">{theme.description}</p>

        {job ? (
          <InlineDownloadButton
            jobId={job.jobId}
            filename={job.filename}
            format={fmt}
          />
        ) : (
          <button
            onClick={() => onGenerate(theme.id)}
            className="mt-auto flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg
                       bg-indigo-600 hover:bg-indigo-700 text-white font-medium
                       transition-colors"
          >
            <Presentation size={11} /> Generate
          </button>
        )}
      </div>
    </div>
  );
}

// ── Main DocWorkflowCard ──────────────────────────────────────────────────────
export default function DocWorkflowCard({ title, fmt, filename, slidesKey, nSlides, themes }) {
  const [jobs, setJobs] = useState({});

  const handleGenerate = async (themeId) => {
    if (jobs[themeId]) return; // already triggered

    // Client-side pre-check — mirrors the server-side validate_identifier()
    // in core/security_validation.py (docs_router.py's POST /docs/generate-themed
    // treats `filename` as an identifier, not free text). The backend remains
    // the authoritative enforcer.
    const themedFilename = filename.replace(/\.pptx$/i, `_${themeId}.pptx`);
    const filenameCheck = validateIdentifier(themedFilename);
    if (!filenameCheck.isValid) {
      console.error("generate-themed rejected: invalid filename —", filenameCheck.errors[0]?.message);
      return;
    }

    try {
      const res = await authFetch("/ainxt/v1/api/docs/generate-themed", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          slides_key: slidesKey,
          theme_id:   themeId,
          fmt,
          title,
          filename: themedFilename,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.job_id) {
        setJobs(j => ({
          ...j,
          [themeId]: {
            jobId:    data.job_id,
            filename: data.filename || filename,
          },
        }));
      }
    } catch (e) {
      console.error("generate-themed failed:", e);
    }
  };

  return (
    <div className="mt-4 p-4 rounded-2xl border border-indigo-100 bg-indigo-50/40 shadow-sm">
      {/* Header */}
      <div className="flex items-center gap-2 mb-1">
        <Presentation size={16} className="text-indigo-600 shrink-0" />
        <p className="text-sm font-semibold text-gray-800">
          Choose a theme for your {nSlides}-slide presentation
        </p>
      </div>
      <p className="text-xs text-gray-500 mb-4 ml-6">
        Each theme uses the same slide content — only the visual style changes.
      </p>

      {/* Theme cards */}
      <div className="flex flex-wrap gap-3">
        {(themes || []).map(theme => (
          <ThemeCard
            key={theme.id}
            theme={theme}
            slidesKey={slidesKey}
            title={title}
            fmt={fmt}
            filename={filename}
            onGenerate={handleGenerate}
            job={jobs[theme.id] || null}
          />
        ))}
      </div>
    </div>
  );
}
