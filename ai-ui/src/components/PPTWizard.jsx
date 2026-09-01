// SPDX-License-Identifier: Apache-2.0
/**
 * PPTWizard — full-screen modal for Presenton-powered presentation generation.
 *
 * Steps:
 *   1. Outline  — LLM-generated slide list the user can edit before generating
 *   2. Theme    — template, slide count, tone, language, verbosity
 *   3. Generate — progress indicator while Presenton works
 *   4. Download — download PPTX / PDF, open in Presenton editor
 *
 * Rendered as a React portal from App.jsx so it sits above all other UI.
 * Props: { prompt, chatId, onClose }
 */

import { useState, useEffect, useRef, useCallback } from "react";
import { createPortal } from "react-dom";
import {
  X,
  Plus,
  Trash2,
  ChevronUp,
  ChevronDown,
  RefreshCw,
  Download,
  ExternalLink,
  Loader2,
  CheckCircle2,
  AlertCircle,
  Presentation,
  Sparkles,
} from "lucide-react";
import { API_BASE as API, authFetch, ENABLE_PRESENTON, PRESENTON_BASE } from "../config";
import { buildPreparePayload, buildExportPayload, buildCreatePayload } from "../lib/presenton-payload";
import { LAYOUT_GROUPS } from "../lib/presenton-layout-registry";
import * as presentonApi from "../lib/presenton-api";
import { presentonLogger } from "../lib/presenton-logger";
import { setLocalData, getLocalData } from "../utils/storageUtils";
import { validateFreeText, validateIdentifier } from "../utils/securityValidation";

// ── Themes from the router catalogue ─────────────────────────────────────────
// Must match LAYOUT_GROUPS in presenton-layout-registry.ts
const FALLBACK_THEMES = [
  { id: "general",  name: "General",  color: "#1A2744", preview: "dark",  description: "General purpose layouts for common presentation elements", icon: "./themes/general.png" },
  // { id: "modern",   name: "Modern",   color: "#1A73E8", preview: "light", description: "Modern layouts with clean design" },
  // { id: "standard", name: "Standard", color: "#374151", preview: "light", description: "Standard professional layouts" },
  { id: "swift",    name: "Swift",    color: "#7C3AED", preview: "dark",  description: "Swift and minimal layouts", icon: "./themes/swift.png" },
];

const TONES       = ["professional", "educational", "casual", "sales_pitch", "funny"];
const VERBOSITIES = ["concise", "standard", "text-heavy"];
const LANGUAGES   = ["English", "Hindi", "Tamil", "Telugu", "Kannada", "Malayalam", "Bengali", "Gujarati"];
const SLIDE_COUNTS = [5, 6, 7, 8, 10, 12, 15];

const STEP_LABELS = ["Outline", "Theme & Options", "Generating", "Download"];

// ── Helper: step progress dots ───────────────────────────────────────────────
function StepProgress({ step }) {
  return (
    <div className="flex items-center gap-2 text-sm">
      {STEP_LABELS.map((label, i) => (
        <span key={i} className="flex items-center gap-1">
          <span
            className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold transition-all
              ${i < step  ? "bg-green-500 text-white" : ""}
              ${i === step ? "bg-blue-600 text-white ring-2 ring-blue-300" : ""}
              ${i > step   ? "bg-gray-200 text-gray-500" : ""}`}
          >
            {i < step ? "✓" : i + 1}
          </span>
          <span className={`hidden sm:inline ${i === step ? "text-blue-700 font-semibold" : "text-gray-400"}`}>
            {label}
          </span>
          {i < STEP_LABELS.length - 1 && (
            <span className={`w-8 h-px mx-1 ${i < step ? "bg-green-400" : "bg-gray-200"}`} />
          )}
        </span>
      ))}
    </div>
  );
}

// ── Step 1: Outline editor ────────────────────────────────────────────────────
function OutlineEditor({ outline, setOutline, prompt, loadingOutline, onRegenerate }) {
  function updateTitle(val) {
    setOutline(o => ({ ...o, title: val }));
  }

  function updateSlide(idx, field, val) {
    setOutline(o => {
      const slides = [...o.slides];
      slides[idx] = { ...slides[idx], [field]: val };
      return { ...o, slides };
    });
  }

  function updateBullet(slideIdx, bIdx, val) {
    setOutline(o => {
      const slides = [...o.slides];
      const bullets = [...slides[slideIdx].bullets];
      bullets[bIdx] = val;
      slides[slideIdx] = { ...slides[slideIdx], bullets };
      return { ...o, slides };
    });
  }

  function addBullet(slideIdx) {
    setOutline(o => {
      const slides = [...o.slides];
      slides[slideIdx] = {
        ...slides[slideIdx],
        bullets: [...slides[slideIdx].bullets, ""],
      };
      return { ...o, slides };
    });
  }

  function removeBullet(slideIdx, bIdx) {
    setOutline(o => {
      const slides = [...o.slides];
      const bullets = slides[slideIdx].bullets.filter((_, i) => i !== bIdx);
      slides[slideIdx] = { ...slides[slideIdx], bullets };
      return { ...o, slides };
    });
  }

  function addSlide() {
    setOutline(o => ({
      ...o,
      slides: [...o.slides, { title: "New Slide", bullets: ["Key point"] }],
    }));
  }

  function removeSlide(idx) {
    setOutline(o => ({
      ...o,
      slides: o.slides.filter((_, i) => i !== idx),
    }));
  }

  function moveSlide(idx, dir) {
    setOutline(o => {
      const slides = [...o.slides];
      const target = idx + dir;
      if (target < 0 || target >= slides.length) return o;
      [slides[idx], slides[target]] = [slides[target], slides[idx]];
      return { ...o, slides };
    });
  }

  if (loadingOutline) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4 text-gray-500">
        <Loader2 className="w-10 h-10 animate-spin text-blue-500" />
        <p className="text-base font-medium">Generating outline with Claude…</p>
        <p className="text-sm text-gray-400">Analysing your topic and structuring slides</p>
      </div>
    );
  }

  if (!outline) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4 text-gray-400">
        <AlertCircle className="w-10 h-10 text-red-400" />
        <p>Failed to generate outline.</p>
        <button
          onClick={onRegenerate}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm"
        >
          <RefreshCw className="w-4 h-4" /> Try again
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 h-full">
      {/* Presentation title */}
      <div className="flex items-center gap-3">
        <label className="text-sm font-semibold text-gray-600 whitespace-nowrap">Title:</label>
        <input
          className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm font-medium focus:outline-none focus:ring-2 focus:ring-blue-400"
          value={outline.title || ""}
          onChange={e => updateTitle(e.target.value)}
          placeholder="Presentation title"
        />
        <button
          onClick={onRegenerate}
          title="Regenerate outline with AI"
          className="flex items-center gap-1.5 px-3 py-2 text-sm bg-gray-100 hover:bg-blue-50 hover:text-blue-700 text-gray-600 rounded-lg border border-gray-200 transition-colors"
        >
          <Sparkles className="w-3.5 h-3.5" />
          <span className="hidden md:inline">Regenerate</span>
        </button>
      </div>

      {/* Slide cards */}
      <div className="flex-1 overflow-y-auto space-y-3 pr-1">
        {(outline.slides || []).map((slide, si) => (
          <div key={si} className="border border-gray-200 rounded-xl p-4 bg-white shadow-sm hover:border-blue-200 transition-colors">
            <div className="flex items-start gap-2 mb-3">
              <span className="flex-shrink-0 w-6 h-6 rounded-full bg-blue-100 text-blue-700 text-xs font-bold flex items-center justify-center mt-0.5">
                {si + 1}
              </span>
              <input
                className="flex-1 text-sm font-semibold border-0 border-b border-transparent hover:border-gray-300 focus:border-blue-400 focus:outline-none pb-0.5 bg-transparent"
                value={slide.title || ""}
                onChange={e => updateSlide(si, "title", e.target.value)}
                placeholder="Slide title"
              />
              <div className="flex items-center gap-0.5 ml-2">
                <button onClick={() => moveSlide(si, -1)} disabled={si === 0}
                  className="p-1 text-gray-400 hover:text-gray-600 disabled:opacity-30">
                  <ChevronUp className="w-4 h-4" />
                </button>
                <button onClick={() => moveSlide(si, 1)} disabled={si === (outline.slides.length - 1)}
                  className="p-1 text-gray-400 hover:text-gray-600 disabled:opacity-30">
                  <ChevronDown className="w-4 h-4" />
                </button>
                <button onClick={() => removeSlide(si)} disabled={outline.slides.length <= 2}
                  className="p-1 text-red-400 hover:text-red-600 disabled:opacity-30 ml-1">
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>

            <div className="space-y-1.5 pl-8">
              {(slide.bullets || []).map((b, bi) => (
                <div key={bi} className="flex items-center gap-2">
                  <span className="text-gray-300 text-sm">•</span>
                  <input
                    className="flex-1 text-sm text-gray-700 border-0 border-b border-transparent hover:border-gray-200 focus:border-blue-300 focus:outline-none bg-transparent py-0.5"
                    value={b}
                    onChange={e => updateBullet(si, bi, e.target.value)}
                    placeholder="Bullet point"
                  />
                  <button onClick={() => removeBullet(si, bi)} disabled={slide.bullets.length <= 1}
                    className="text-gray-300 hover:text-red-400 disabled:opacity-20 p-0.5">
                    <X className="w-3 h-3" />
                  </button>
                </div>
              ))}
              <button
                onClick={() => addBullet(si)}
                className="text-xs text-blue-500 hover:text-blue-700 flex items-center gap-1 mt-1 pl-4"
              >
                <Plus className="w-3 h-3" /> Add bullet
              </button>
            </div>
          </div>
        ))}
      </div>

      <button
        onClick={addSlide}
        className="flex items-center justify-center gap-2 w-full py-2.5 border-2 border-dashed border-gray-300 rounded-xl text-gray-500 hover:border-blue-400 hover:text-blue-600 text-sm transition-colors"
      >
        <Plus className="w-4 h-4" /> Add Slide
      </button>
    </div>
  );
}

// ── Step 2: Theme & Options ───────────────────────────────────────────────────
function ThemeSelector({ themes, selectedTheme, setSelectedTheme, options, setOptions }) {
  return (
    <div className="flex flex-col gap-6 h-full overflow-y-auto">
      {/* Theme grid */}
      <div>
        <p className="text-sm font-semibold text-gray-700 mb-3">Choose a Theme</p>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {themes.map(t => (
            <button
              key={t.id}
              onClick={() => setSelectedTheme(t.id)}
              className={`relative rounded-xl border-2 p-4 text-left transition-all
                ${selectedTheme === t.id
                  ? "border-blue-500 shadow-md shadow-blue-100"
                  : "border-gray-200 hover:border-blue-300"}`}
            >
              {/* Theme icon image */}
              <div className="w-full h-32 rounded-lg mb-3 overflow-hidden bg-gray-50 flex items-center justify-center">
                {t.icon ? (
                  <img 
                    src={t.icon} 
                    alt={`${t.name} theme preview`}
                    className="w-full h-full object-cover"
                    onError={(e) => {
                      // Fallback to color swatch if image fails to load
                      e.target.style.display = 'none';
                      e.target.parentElement.style.background = `linear-gradient(135deg, ${t.color}ee, ${t.color}99)`;
                    }}
                  />
                ) : (
                  <div
                    className="w-full h-full flex items-end p-2"
                    style={{ background: `linear-gradient(135deg, ${t.color}ee, ${t.color}99)` }}
                  >
                    <div className="flex gap-1">
                      {[1,2,3].map(i => (
                        <div key={i}
                          className="h-1.5 rounded-full bg-white opacity-70"
                          style={{ width: i === 1 ? "40%" : i === 2 ? "30%" : "20%" }}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>
              <p className="text-sm font-semibold text-gray-800">{t.name}</p>
              <p className="text-xs text-gray-500 mt-0.5">{t.description}</p>
              {selectedTheme === t.id && (
                <CheckCircle2 className="absolute top-2 right-2 w-5 h-5 text-blue-500" />
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Options grid */}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Slides</label>
          <select
            className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
            value={options.n_slides}
            onChange={e => setOptions(o => ({ ...o, n_slides: Number(e.target.value) }))}
          >
            {SLIDE_COUNTS.map(n => <option key={n} value={n}>{n} slides</option>)}
          </select>
        </div>

        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Tone</label>
          <select
            className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
            value={options.tone}
            onChange={e => setOptions(o => ({ ...o, tone: e.target.value }))}
          >
            {TONES.map(t => <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1).replace("_", " ")}</option>)}
          </select>
        </div>

        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Language</label>
          <select
            className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
            value={options.language}
            onChange={e => setOptions(o => ({ ...o, language: e.target.value }))}
          >
            {LANGUAGES.map(l => <option key={l} value={l}>{l}</option>)}
          </select>
        </div>

        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Verbosity</label>
          <select
            className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
            value={options.verbosity}
            onChange={e => setOptions(o => ({ ...o, verbosity: e.target.value }))}
          >
            {VERBOSITIES.map(v => <option key={v} value={v}>{v.charAt(0).toUpperCase() + v.slice(1).replace("-", " ")}</option>)}
          </select>
        </div>
      </div>

      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          className="w-4 h-4 text-blue-600 rounded"
          checked={options.include_table_of_contents}
          onChange={e => setOptions(o => ({ ...o, include_table_of_contents: e.target.checked }))}
        />
        <span className="text-sm text-gray-700">Include table of contents slide</span>
      </label>

      {/* Format toggle */}
      <div>
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Export Format</p>
        <div className="flex gap-2">
          {["pptx", "pdf"].map(fmt => (
            <button
              key={fmt}
              onClick={() => setOptions(o => ({ ...o, export_as: fmt }))}
              className={`flex-1 py-2 rounded-lg text-sm font-semibold border-2 transition-all
                ${options.export_as === fmt
                  ? "border-blue-500 bg-blue-50 text-blue-700"
                  : "border-gray-200 text-gray-500 hover:border-gray-300"}`}
            >
              {fmt.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Step 3: Generation progress ───────────────────────────────────────────────
function GeneratingStep({ progress, streamStatus, streamMessage }) {
  const steps = [
    "Sending outline to presentation engine…",
    "Applying theme and layout…",
    "Generating slide content with AI…",
    "Rendering slides…",
    "Exporting to PPTX…",
  ];
  const activeIdx = Math.min(Math.floor((progress / 100) * steps.length), steps.length - 1);

  // Simplified status message for user - no technical details
  const getUserFriendlyMessage = () => {
    switch (streamStatus) {
      case 'connecting':
        return "Connecting to presentation engine...";
      case 'connected':
      case 'generating':
        return "Generating your presentation...";
      case 'completed':
        return "Finalizing...";
      case 'failed':
        return "Generation failed";
      case 'aborted':
        return "Generation cancelled";
      default:
        return "Preparing...";
    }
  };

  // Only show status indicator for non-normal states
  const showStatusIndicator = streamStatus && streamStatus !== 'connected';

  return (
    <div className="flex flex-col items-center justify-center h-full gap-8 py-12">
      <div className="relative w-24 h-24 flex items-center justify-center">
        <Loader2 className="w-16 h-16 text-blue-500 animate-spin" />
      </div>

      <div className="space-y-2 w-full max-w-sm">
        {steps.map((s, i) => (
          <div key={i} className={`flex items-center gap-3 text-sm transition-all
            ${i < activeIdx  ? "text-green-600" : ""}
            ${i === activeIdx ? "text-blue-700 font-semibold" : ""}
            ${i > activeIdx  ? "text-gray-300" : ""}`}
          >
            <span className="flex-shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-xs">
              {i < activeIdx  ? "✓" : i === activeIdx ? <Loader2 className="w-3 h-3 animate-spin" /> : "○"}
            </span>
            {s}
          </div>
        ))}
      </div>

      {/* Simplified status message - no retry details */}
      {showStatusIndicator && (
        <div className="flex items-center gap-2 px-4 py-2 bg-gray-50 rounded-lg border border-gray-200">
          {streamStatus === 'failed' ? (
            <AlertCircle className="w-4 h-4 text-red-500" />
          ) : (
            <div className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
          )}
          <span className={`text-xs ${streamStatus === 'failed' ? 'text-red-600' : 'text-gray-600'}`}>
            {getUserFriendlyMessage()}
          </span>
        </div>
      )}

      <p className="text-xs text-gray-400 text-center max-w-xs">
        AiNxt Presenton is generating your slides.
        This takes 60–120 seconds for a full deck.
      </p>
    </div>
  );
}

// ── Step 4: Download ──────────────────────────────────────────────────────────
function DownloadStep({ jobId, result, prompt, options, onClose, user }) {
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState(null);

  async function handleDownload() {
    setDownloading(true);
    setDownloadError(null);
    try {
      let blob;
      if (ENABLE_PRESENTON) {
        // Use Presenton export endpoint
        const title = result?.title || result?.slides?.[0]?.content?.title || prompt.slice(0, 80);
        
        // First update the presentation with latest data
        const updatePayload = {
          id: jobId,
          title: title,
          n_slides: result?.n_slides || result?.slides?.length || 1,
          slides: result?.slides || []
        };
        await presentonApi.updatePresentation(updatePayload, user?.email);
        
        // Then export with selected format
        const exportPayload = buildExportPayload(jobId, title);
        const format = options.export_as || 'pptx';
        blob = await presentonApi.exportPresentation(exportPayload, format, user?.email, user?.role);
      } else {
        const r = await authFetch(`${API}/ppt/download/${jobId}`);
        if (!r.ok) throw new Error(`Download failed: HTTP ${r.status}`);
        blob = await r.blob();
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = result?.filename || "presentation.pptx";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e) {
      setDownloadError(e.message || 'Download failed');
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="flex flex-col items-center justify-center h-full gap-8 py-8">
      <div className="flex flex-col items-center gap-3">
        <div className="w-20 h-20 rounded-full bg-green-100 flex items-center justify-center">
          <CheckCircle2 className="w-10 h-10 text-green-500" />
        </div>
        <h3 className="text-xl font-bold text-gray-800">Presentation Ready!</h3>
        <p className="text-sm text-gray-500 text-center max-w-sm">
          Your presentation on <em>"{prompt.slice(0, 60)}{prompt.length > 60 ? "…" : ""}"</em> has been generated successfully.
        </p>
      </div>

      {downloadError && (
        <div className="flex items-center gap-2 text-red-600 text-sm bg-red-50 border border-red-200 rounded-lg px-4 py-2 max-w-sm w-full">
          <AlertCircle className="w-4 h-4 flex-shrink-0" />
          <span>{downloadError}</span>
        </div>
      )}

      <div className="flex flex-col sm:flex-row gap-3 w-full max-w-sm">
        <button
          onClick={handleDownload}
          disabled={downloading}
          className="flex-1 flex items-center justify-center gap-2 py-3 px-6 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white rounded-xl font-semibold text-sm transition-colors"
        >
          {downloading
            ? <><Loader2 className="w-4 h-4 animate-spin" /> Downloading…</>
            : <><Download className="w-4 h-4" /> Download PPTX</>
          }
        </button>

        {/* Show Presenton editor link when Presenton is enabled */}
        {ENABLE_PRESENTON && jobId && (
          <a
            href={`${PRESENTON_BASE}/dashboard`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-center gap-2 py-3 px-5 border-2 border-gray-200 hover:border-blue-300 text-gray-700 hover:text-blue-700 rounded-xl font-semibold text-sm transition-colors"
          >
            <ExternalLink className="w-4 h-4" /> Edit in Presenton
          </a>
        )}

        {/* Legacy edit_url fallback */}
        {/* {!ENABLE_PRESENTON && result?.edit_url && (
          <a
            href={result.edit_url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-center gap-2 py-3 px-5 border-2 border-gray-200 hover:border-blue-300 text-gray-700 hover:text-blue-700 rounded-xl font-semibold text-sm transition-colors"
          >
            <ExternalLink className="w-4 h-4" /> Edit in Presenton
          </a>
        )} */}
      </div>

      <div className="flex gap-3">
        <button
          onClick={onClose}
          className="text-sm text-gray-400 hover:text-gray-600 transition-colors"
        >
          Close wizard
        </button>
      </div>

      <div className="bg-blue-50 border border-blue-100 rounded-xl p-4 text-xs text-blue-700 max-w-sm text-center">
        {/* <strong>Tip:</strong> Click "Edit in Presenton" to open the full Presenton editor
        where you can change individual slides, fonts, and layouts before exporting. */}
      </div>
    </div>
  );
}


// ── Main wizard ───────────────────────────────────────────────────────────────

export default function PPTWizard({ prompt, chatId, onClose, onComplete, user }) {
  // Debug: Log user information
  
  const [step, setStep]             = useState(0);
  const [outline, setOutline]       = useState(null);
  const [loadingOutline, setLoadingOutline] = useState(false);
  const [outlineError, setOutlineError]     = useState(null);
  const [themes, setThemes]         = useState(FALLBACK_THEMES);
  const [selectedTheme, setSelectedTheme]   = useState("general");
  const [options, setOptions]       = useState({
    n_slides:                  8,
    tone:                      "professional",
    language:                  "English",
    verbosity:                 "standard",
    include_table_of_contents: false,
    export_as:                 "pptx",
  });
  const [jobId, setJobId]           = useState(null);
  const [genError, setGenError]     = useState(null);
  const [genProgress, setGenProgress] = useState(5);
  const [result, setResult]         = useState(null);
  const [streamStatus, setStreamStatus] = useState(null);
  const [streamMessage, setStreamMessage] = useState('');
  const pollRef = useRef(null);
  const abortControllerRef = useRef(null);

  // Load themes from local registry (no API calls)
  useEffect(() => {
    const templateGroups = ['general', 'swift'];
    const registryThemes = templateGroups.map(group => {
      const groupData = LAYOUT_GROUPS[group];
      const fallback = FALLBACK_THEMES.find(t => t.id === group);
      return {
        id: group,
        name: group.charAt(0).toUpperCase() + group.slice(1),
        color: fallback?.color || '#1A2744',
        preview: fallback?.preview || 'dark',
        description: `${groupData?.slides?.length || 0} slide layouts available`,
        icon: fallback?.icon, // Preserve icon from FALLBACK_THEMES
        // Store registry data for later use
        _templateData: groupData ? {
          name: groupData.name,
          ordered: groupData.ordered,
          slides: groupData.slides
        } : null
      };
    });
    
    setThemes(registryThemes);
    
    // Store in cache for compatibility
    try {
      localStorage.setItem('presenton_template_cache', JSON.stringify(registryThemes));
    } catch (_) { /* ignore */ }
  }, []);

  // Generate outline on mount
  const fetchOutline = useCallback(async () => {
    setLoadingOutline(true);
    setOutlineError(null);
    // Client-side pre-check mirroring validate_presenton_outline_request() in
    // core/security_validation.py — prompt is mandatory free text. Backend
    // remains the authoritative enforcer.
    if (!prompt || !prompt.trim()) {
      setOutlineError("Prompt is required");
      setLoadingOutline(false);
      return;
    }
    const promptCheck = validateFreeText(prompt);
    if (!promptCheck.isValid) {
      setOutlineError(promptCheck.errors[0]?.message || "Invalid prompt");
      setLoadingOutline(false);
      return;
    }
    try {
      const r = await authFetch(`${API}/ppt/outline`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ prompt, n_slides: options.n_slides }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setOutline(data);
    } catch (e) {
      setOutlineError(e.message);
      setOutline(null);
    } finally {
      setLoadingOutline(false);
    }
  }, [prompt, options.n_slides]);

  useEffect(() => { fetchOutline(); }, []); // eslint-disable-line

  // Poll for generation result
  useEffect(() => {
    if (step !== 2 || !jobId) return;

    // Track polling start time for timeout
    const pollStartTime = Date.now();
    const MAX_POLL_DURATION = 600000; // 10 minutes max polling
    let consecutiveErrors = 0;
    const MAX_CONSECUTIVE_ERRORS = 10; // Allow up to 10 consecutive errors before giving up

    // Animate progress up to 90% while waiting
    const progressInterval = setInterval(() => {
      setGenProgress(p => (p < 88 ? p + Math.random() * 2 : p));
    }, 3000);

    pollRef.current = setInterval(() => {
      (async () => {
        try {
          // Check for overall timeout
          if (Date.now() - pollStartTime > MAX_POLL_DURATION) {
            clearInterval(pollRef.current);
            clearInterval(progressInterval);
            setGenError("Generation timed out after 10 minutes. Please try again.");
            return;
          }

          if (ENABLE_PRESENTON) {
            // Use Presenton metadata endpoint for progress and completion detection
          const m = await presentonApi.fetchMetadata(jobId, user?.email);
            
            // Reset error counter on successful fetch
            consecutiveErrors = 0;
            
            if (m && m.slides && m.slides.length > 0 && m.slides.every(s => s.content && Object.keys(s.content).length > 0)) {
              clearInterval(pollRef.current);
              clearInterval(progressInterval);
              setGenProgress(100);
              setStreamStatus('completed');
              setStreamMessage('Generation complete!');
              setResult(m);
              // Notify parent component that presentation is complete
              if (onComplete) {
                onComplete({
                  id: jobId,
                  title: m.title || prompt.slice(0, 80),
                  n_slides: m.n_slides || m.slides.length,
                  format: options.export_as || 'pptx'
                });
              }
              setTimeout(() => setStep(3), 600);
              return;
            }
            // Update heuristic progress based on how many slides have content
            const genCount = m.slides ? m.slides.filter(s => s.content && Object.keys(s.content).length > 0).length : 0;
            const progress = m.n_slides ? Math.min(90, (genCount / m.n_slides) * 100) : Math.min(90, genCount * 10);
            setGenProgress(Math.max(5, progress));
          } else {
            const r = await authFetch(`${API}/ppt/status/${jobId}`);
            if (!r.ok) return;
            const d = await r.json();
            if (d.status === "done") {
              clearInterval(pollRef.current);
              clearInterval(progressInterval);
              setGenProgress(100);
              setResult(d);
              setTimeout(() => setStep(3), 600);
            } else if (d.status === "error") {
              clearInterval(pollRef.current);
              clearInterval(progressInterval);
              setGenError(d.error || "Generation failed");
              setStep(2); // stay on step 2 to show error
            }
          }
        } catch (e) {
          // Log error but continue polling - don't let transient network errors stop polling
          consecutiveErrors++;
          console.warn(`[Presenton Polling] Error ${consecutiveErrors}/${MAX_CONSECUTIVE_ERRORS}:`, e.message || e);
          
          // Only stop if we've had too many consecutive errors
          if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
            clearInterval(pollRef.current);
            clearInterval(progressInterval);
            setGenError(`Network error: Unable to check status after ${MAX_CONSECUTIVE_ERRORS} attempts. Please check your connection and try again.`);
            setStreamStatus('failed');
          }
          // Otherwise continue polling - the next attempt might succeed
        }
      })();
    }, 3000);

    return () => {
      clearInterval(pollRef.current);
      clearInterval(progressInterval);
    };
  }, [step, jobId]);

  async function handleGenerate() {
    // Client-side pre-check mirroring validate_presenton_generate_request() in
    // core/security_validation.py — prompt is mandatory free text, template
    // is an identifier (selects a template dir server-side). Backend remains
    // the authoritative enforcer.
    const promptCheck = validateFreeText(prompt || "");
    if (!prompt || !prompt.trim() || !promptCheck.isValid) {
      setGenError(promptCheck.errors[0]?.message || "Prompt is required");
      return;
    }
    const templateCheck = validateIdentifier(selectedTheme || "general");
    if (!templateCheck.isValid) {
      setGenError(templateCheck.errors[0]?.message || "Invalid template");
      return;
    }

    setStep(2);
    setGenError(null);
    setGenProgress(5);
    setStreamStatus('connecting');
    setStreamMessage('Initializing presentation...');

    // Create abort controller for this generation
    abortControllerRef.current = new AbortController();

    // Keep building the rich content for backwards-compatibility / debug
    const slides = outline?.slides || [];
    const contentParts = [
      `Topic: ${outline?.title || prompt}`,
      `Number of slides: ${slides.length}`,
      "",
      ...slides.map((s, i) => {
        const lines = [`Slide ${i + 1}: ${s.title}`];
        (s.bullets || []).forEach(b => lines.push(`  - ${b}`));
        const chart = s.chart;
        if (chart?.type && chart.type !== "none" && chart.labels?.length) {
          lines.push(`  Chart (${chart.type}): ${chart.title || ""}`);
          chart.labels.forEach((lbl, li) =>
            lines.push(`    ${lbl}: ${chart.values?.[li] ?? ""}`)
          );
        }
        (s.stats || []).forEach(st =>
          lines.push(`  Metric: ${st.value} — ${st.label}${st.delta ? " (" + st.delta + ")" : ""}`)
        );
        return lines.join("\n");
      }),
    ];

    if (ENABLE_PRESENTON) {
      try {
        // Step 1: Create presentation first (required before prepare)
        setStreamStatus('connecting');
        setStreamMessage('Creating presentation...');
        
        const createPayload = buildCreatePayload(prompt, {
          n_slides: slides.length,
          language: options.language,
          tone: options.tone,
          verbosity: options.verbosity,
          include_table_of_contents: options.include_table_of_contents,
          include_title_slide: options.include_title_slide,
          user_id: user?.email,
        });
        
        const createResp = await presentonApi.createPresentation(createPayload, user?.email);
        const presentationId = createResp?.id;
        
        if (!presentationId) {
          throw new Error('Create response did not include presentation id');
        }


        // Step 2: Wait a bit for the presentation to be fully saved, then prepare
        setStreamStatus('connecting');
        setStreamMessage('Preparing slides...');
        
        // Add delay to ensure presentation is fully saved in database
        await new Promise(r => setTimeout(r, 1000));
        
        // Get layout from selected theme (from local registry)
        const selectedThemeData = themes.find(t => t.id === selectedTheme);
        const layoutFromRegistry = selectedThemeData?._templateData;
        
        if (!layoutFromRegistry) {
          throw new Error(`No layout found for theme "${selectedTheme}" in local registry`);
        }
        
        // Build prepare payload using local registry layout
        const preparePayload = buildPreparePayload(
          { title: outline?.title || prompt, slides },
          { ...options, selectedTheme },
          presentationId,
          layoutFromRegistry
        );
        
        
        
        const prepareResp = await presentonApi.prepare(preparePayload, user?.email);
        
        
        // Step 3: Start the SSE stream to generate slide content with status tracking
        setStreamStatus('connected');
        setStreamMessage('Generating content...');
        
        // Start stream with status callbacks
        presentonApi.streamPresentation(
          presentationId,
          {
            onStatusChange: (status, message) => {
              setStreamStatus(status);
              setStreamMessage(message);
            },
            maxRetries: 3
          },
          abortControllerRef.current.signal,
          user?.email
        ).then(streamResult => {
          if (!streamResult) {
            console.warn('[Presenton] Stream did not complete successfully, but polling will continue');
          }
        }).catch(err => {
          console.warn('[Presenton] Stream error:', err);
          setStreamStatus('failed');
          setStreamMessage('Stream failed, checking progress...');
        });
        
        // Use the same ID for polling
        setJobId(presentationId);
        try {
          setLocalData('presenton_presentation_id', String(presentationId || '').replace(/[^a-zA-Z0-9_\-]/g, ''));
        } catch (_) { /* ignore */ }
      } catch (e) {
        setStreamStatus('failed');
        setStreamMessage(e.message);
        setGenError(e.message);
      }
    } else {
      // Legacy backend path
      try {
        const content = contentParts.join("\n");
        const r = await authFetch(`${API}/ppt/generate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            content,
            theme: selectedTheme,
            ...options,
          }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        setJobId(d.job_id || d.id);
      } catch (e) {
        setGenError(e.message);
      }
    }
  }

  // Close on Escape
  useEffect(() => {
    function onKey(e) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const canProceedStep0 = !loadingOutline && outline && (outline.slides?.length >= 2);
  const canProceedStep1 = true;

  const modal = (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(4px)" }}
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className="bg-white rounded-2xl shadow-2xl flex flex-col overflow-hidden"
        style={{ width: "min(960px, 95vw)", height: "min(820px, 95vh)" }}
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-blue-600 flex items-center justify-center">
              <Presentation className="w-5 h-5 text-white" />
            </div>
            <div>
              <h2 className="text-base font-bold text-gray-800">AiNxt Presentation Studio</h2>
              <p className="text-xs text-gray-400 truncate max-w-[380px]" title={prompt}>{prompt}</p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <StepProgress step={step} />
            <button
              onClick={onClose}
              className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-hidden px-6 py-5">
          {step === 0 && (
            <OutlineEditor
              outline={outline}
              setOutline={setOutline}
              prompt={prompt}
              loadingOutline={loadingOutline}
              onRegenerate={fetchOutline}
            />
          )}
          {step === 1 && (
            <ThemeSelector
              themes={themes}
              selectedTheme={selectedTheme}
              setSelectedTheme={setSelectedTheme}
              options={options}
              setOptions={setOptions}
            />
          )}
          {step === 2 && (
            genError
              ? (
                <div className="flex flex-col items-center justify-center h-full gap-4 text-red-600">
                  <AlertCircle className="w-12 h-12" />
                  <p className="text-base font-semibold">Generation failed</p>
                  <p className="text-sm text-gray-500">{genError}</p>
                  <button
                    onClick={() => { setStep(1); setGenError(null); }}
                    className="mt-2 px-5 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700"
                  >
                    Go back and retry
                  </button>
                </div>
              )
              : <GeneratingStep 
                  progress={genProgress} 
                  streamStatus={streamStatus}
                  streamMessage={streamMessage}
                />
          )}
          {step === 3 && (
            <DownloadStep
              jobId={jobId}
              result={result}
              prompt={prompt}
              options={options}
              onClose={onClose}
              user={user}
            />
          )}
        </div>

        {/* Footer nav (hidden on generate/download steps) */}
        {step < 2 && (
          <div className="flex items-center justify-between px-6 py-4 border-t border-gray-100 bg-gray-50">
            <button
              onClick={() => step > 0 && setStep(s => s - 1)}
              disabled={step === 0}
              className="px-5 py-2.5 text-sm font-semibold text-gray-600 hover:text-gray-800 hover:bg-gray-100 rounded-lg disabled:opacity-30 transition-colors"
            >
              ← Back
            </button>

            <div className="text-xs text-gray-400">
              {step === 0 && `${outline?.slides?.length || 0} slides`}
              {step === 1 && `Theme: ${themes.find(t => t.id === selectedTheme)?.name || selectedTheme}`}
            </div>

            {step < 1 && (
              <button
                onClick={() => setStep(s => s + 1)}
                disabled={!canProceedStep0}
                className="px-6 py-2.5 text-sm font-semibold bg-blue-600 hover:bg-blue-700 disabled:bg-blue-300 text-white rounded-lg transition-colors"
              >
                Next: Theme & Options →
              </button>
            )}
            {step === 1 && (
              <button
                onClick={handleGenerate}
                disabled={!canProceedStep1}
                className="flex items-center gap-2 px-6 py-2.5 text-sm font-semibold bg-blue-600 hover:bg-blue-700 disabled:bg-blue-300 text-white rounded-lg transition-colors"
              >
                <Sparkles className="w-4 h-4" />
                Generate Presentation
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
