// SPDX-License-Identifier: Apache-2.0
import { useMemo } from "react";
import { X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * DocLivePreview — CoWorker / Claude-artifacts-style live document preview.
 *
 * Replaces DocGenSpinner once the LLM begins streaming structured content.
 * Sections (heading + body + bullets + callout) materialize one-by-one as
 * each becomes parseable in the streaming JSON; the most recent section
 * shows a blinking cursor. Once the LLM is done, the bottom strip switches
 * from "Drafting…" to a "Building <FORMAT>…" indicator while the file is
 * rendered server-side.
 *
 * Props:
 *   progress    — { step, total_steps, label, detail } from polling endpoint
 *   livePreview — { title, domain, sections: [{heading, content, bullets,
 *                   callout: {label, text}}], total_hint, done }
 *   format      — "pdf" | "docx" | "pptx" | "xlsx" | "md"
 *   mode        — "generate" | "edit"
 *   onCancel    — cancel-button handler
 *   cancelling  — boolean (disables the cancel button while cancellation
 *                 round-trips to the server)
 */
export default function DocLivePreview({
  progress,
  livePreview,
  format = "document",
  mode,
  onCancel,
  cancelling,
}) {
  const sections   = livePreview?.sections || [];
  const totalHint  = Math.max(livePreview?.total_hint || 0, sections.length);
  const fmtLabel   = (format || "document").toUpperCase();
  const actionVerb = mode === "edit" ? "Editing" : "Generating";
  const titleIcon  = mode === "edit" ? "\u270F\uFE0F" : "\uD83D\uDCC4";

  // Once the LLM finishes streaming, the worker advances past structuring
  // and the server is building the binary file. Drive that distinction
  // off `livePreview.done` (set by the worker when the final sections
  // snapshot is published).
  const drafting = !livePreview?.done;
  const buildStepLabel = progress?.label || "";

  // Outline placeholders for not-yet-streamed sections.
  const placeholderCount = Math.max(0, totalHint - sections.length);
  const placeholders = useMemo(
    () => Array.from({ length: placeholderCount }, (_, i) => i),
    [placeholderCount],
  );

  const docTitle = livePreview?.title?.trim() || "";

  return (
    <div className="relative w-full max-w-2xl my-2">
      <div className="relative p-5 rounded-2xl backdrop-blur-xl bg-white/10
                      border border-white/20 shadow-[0_8px_32px_rgba(0,0,0,0.18)]">
        <div className="absolute inset-0 overflow-hidden rounded-2xl pointer-events-none">
          <div className="absolute w-64 h-64 bg-indigo-500/15 blur-3xl animate-pulse
                          top-[-40px] left-[-40px]" />
          <div className="absolute w-64 h-64 bg-violet-500/15 blur-3xl animate-pulse
                          bottom-[-40px] right-[-40px]" />
        </div>

        {/* Header */}
        <div className="relative z-10 mb-3 flex items-start justify-between gap-3">
          <div className="flex flex-col min-w-0">
            <span className="text-sm font-semibold text-gray-700
                             dark:text-white/80 tracking-wide">
              {titleIcon} {actionVerb} {fmtLabel}…
            </span>
            {docTitle && (
              <span className="mt-0.5 text-base font-bold text-indigo-700
                               dark:text-indigo-200 truncate" title={docTitle}>
                {docTitle}
              </span>
            )}
          </div>
          <button
            onClick={onCancel}
            disabled={cancelling}
            className="flex items-center justify-center w-7 h-7 rounded-full
                       bg-white/80 backdrop-blur-md border border-red-200
                       text-red-500 hover:bg-red-50 hover:border-red-400
                       hover:scale-110 transition-all duration-200
                       shadow-sm hover:shadow-md disabled:opacity-50 shrink-0"
            title="Cancel generation"
          >
            <X size={14} />
          </button>
        </div>

        {/* Section list */}
        <div className="relative z-10 max-h-96 overflow-y-auto pr-1 space-y-3
                        rounded-lg bg-white/40 dark:bg-black/30
                        border border-gray-200/40 dark:border-white/10 p-3">
          {sections.length === 0 && placeholders.length === 0 && (
            <div className="text-xs text-gray-500 dark:text-white/40 italic">
              Waiting for the model to start drafting…
            </div>
          )}

          {sections.map((sec, idx) => {
            const isLast = idx === sections.length - 1 && drafting;
            const heading = sec.heading || `Section ${idx + 1}`;
            // Body: trim to first ~6 lines for the preview so a single very
            // long section doesn't push the rest off-screen.
            const body = String(sec.content || "")
              .split("\n")
              .filter(Boolean)
              .slice(0, 6)
              .join("\n");
            const bullets = Array.isArray(sec.bullets) ? sec.bullets.slice(0, 4) : [];
            const callout = sec.callout && (sec.callout.text || sec.callout.label)
              ? sec.callout : null;

            return (
              <div key={idx} className="flex gap-2">
                <span className="mt-1 w-2 h-2 rounded-full bg-green-400
                                 shrink-0 shadow shadow-green-400/50" />
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-semibold text-gray-500
                                  dark:text-white/50 uppercase tracking-wider">
                    {idx + 1}.
                  </div>
                  <div className="text-sm font-bold text-gray-800
                                  dark:text-white/90 mb-1">
                    {heading}
                    {isLast && (
                      <span className="ml-1 inline-block w-2 h-4 align-text-bottom
                                       bg-indigo-400 animate-pulse" />
                    )}
                  </div>
                  {body && (
                    <div className="text-xs text-gray-700 dark:text-white/70
                                    prose prose-sm prose-invert max-w-none
                                    leading-snug">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {body}
                      </ReactMarkdown>
                    </div>
                  )}
                  {bullets.length > 0 && (
                    <ul className="mt-1 ml-3 list-disc text-xs text-gray-600
                                   dark:text-white/60 space-y-0.5">
                      {bullets.map((b, bi) => (
                        <li key={bi} className="leading-snug">{b}</li>
                      ))}
                    </ul>
                  )}
                  {callout && (
                    <div className="mt-1.5 text-[11px] px-2 py-1 rounded
                                    bg-indigo-100/60 dark:bg-indigo-900/40
                                    border-l-2 border-indigo-400
                                    text-indigo-800 dark:text-indigo-200">
                      <span className="font-semibold">
                        {callout.label || "Note"}:
                      </span>{" "}
                      {callout.text}
                    </div>
                  )}
                </div>
              </div>
            );
          })}

          {placeholders.map((i) => (
            <div key={`ph-${i}`} className="flex gap-2 opacity-50">
              <span className="mt-1 w-2 h-2 rounded-full border border-gray-400
                               dark:border-white/30 bg-transparent shrink-0" />
              <div className="text-xs text-gray-400 dark:text-white/30 italic
                              flex-1 truncate">
                Section {sections.length + i + 1} — coming up…
              </div>
            </div>
          ))}
        </div>

        {/* Footer strip */}
        <div className="relative z-10 mt-3 flex items-center justify-between
                        text-xs text-gray-600 dark:text-white/60">
          <span>
            Sections: <strong>{sections.length}</strong>
            {totalHint > sections.length ? <span className="opacity-70">{` / ~${totalHint}`}</span> : null}
          </span>
          <span className="flex items-center gap-2">
            <span className="flex gap-1">
              <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce"
                    style={{ animationDelay: "0ms" }} />
              <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce"
                    style={{ animationDelay: "150ms" }} />
              <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce"
                    style={{ animationDelay: "300ms" }} />
            </span>
            {drafting
              ? (buildStepLabel || "Drafting\u2026")
              : `Building ${fmtLabel}\u2026 ${buildStepLabel || ""}`}
          </span>
        </div>
      </div>
    </div>
  );
}
