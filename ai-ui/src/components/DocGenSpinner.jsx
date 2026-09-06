// SPDX-License-Identifier: MIT
import BrandMark from "./BrandMark";

/**
 * DocGenSpinner — live document-generation progress, Buddy-style.
 *
 * Shows: brand mark + action verb + backend phase label, a step X/N + elapsed
 * clock line, a live section outline that fills in as the model drafts, and a
 * running character count — so the user sees real-time progress instead of a
 * static "Generating…".
 *
 * Props:
 *   progress    — { step, total_steps, label, detail } from the polling endpoint.
 *   livePreview — { title, sections:[{heading, content, bullets}], done } as the
 *                 doc is drafted section-by-section (null until first update).
 *   elapsed     — seconds since the job started (number).
 *   format      — "pdf" | "docx" | "pptx" | "md" …
 *   mode        — "generate" | "edit" | undefined.
 *   onCancel / cancelling — cancel control.
 */
export default function DocGenSpinner({
  progress, livePreview, elapsed = 0, format = "document", mode, onCancel, cancelling,
}) {
  const fmtLabel   = (format || "document").toUpperCase();
  const actionVerb = mode === "edit" ? "Editing" : "Generating";
  const detail     = progress?.label || progress?.detail || "";

  const step   = progress?.step;
  const total  = progress?.total_steps;
  const hasStep = Number.isFinite(step) && Number.isFinite(total) && total > 0;

  const sections = Array.isArray(livePreview?.sections) ? livePreview.sections : [];
  // Running character count across drafted sections (live "chars written").
  const chars = sections.reduce((n, s) => {
    const body    = typeof s?.content === "string" ? s.content.length : 0;
    const heading = typeof s?.heading === "string" ? s.heading.length : 0;
    const bullets = Array.isArray(s?.bullets)
      ? s.bullets.reduce((b, x) => b + (typeof x === "string" ? x.length : 0), 0)
      : 0;
    return n + body + heading + bullets;
  }, 0);

  const mm = String(Math.floor(elapsed / 60)).padStart(1, "0");
  const ss = String(elapsed % 60).padStart(2, "0");

  return (
    <div className="my-2 text-xs text-gray-500">
      {/* ── Status line ─────────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        <BrandMark className="w-4 h-4 brand-breathe shrink-0" />
        <span className="text-gray-600 font-medium">
          {actionVerb} {fmtLabel}{detail ? ` — ${detail}` : ""}…
        </span>
        {onCancel && (
          <button
            onClick={onCancel}
            disabled={cancelling}
            className="ml-1 text-gray-400 hover:text-red-500 disabled:opacity-50
                       cursor-pointer underline-offset-2 hover:underline"
          >
            {cancelling ? "Cancelling…" : "Cancel"}
          </button>
        )}
      </div>

      {/* ── Metrics line: step • elapsed • chars ─────────────────── */}
      <div className="mt-1 ml-6 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-gray-400 tabular-nums">
        {hasStep && <span>Step {step}/{total}</span>}
        <span>{mm}:{ss}</span>
        {chars > 0 && <span>{chars.toLocaleString()} chars</span>}
        {sections.length > 0 && (
          <span>{sections.length} section{sections.length !== 1 ? "s" : ""}</span>
        )}
      </div>

      {/* ── Live section outline (fills in as the model drafts) ──── */}
      {sections.length > 0 && (
        <div className="mt-1.5 ml-6 border-l-2 border-indigo-100 pl-3 space-y-0.5 max-h-40 overflow-hidden">
          {sections.slice(-8).map((s, i) => (
            <div key={i} className="flex items-center gap-1.5 text-[11px] text-gray-500 truncate">
              <span className="inline-block w-1 h-1 rounded-full bg-indigo-400 shrink-0" />
              <span className="truncate">{s?.heading || "Untitled section"}</span>
            </div>
          ))}
          {!livePreview?.done && (
            <div className="flex items-center gap-1.5 text-[11px] text-indigo-400">
              <span className="inline-block w-1 h-1 rounded-full bg-indigo-300 animate-pulse shrink-0" />
              <span className="italic">writing…</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
