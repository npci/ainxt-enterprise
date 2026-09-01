// SPDX-License-Identifier: Apache-2.0
import { useState } from "react";
import { ChevronDown, ChevronUp, FileText } from "lucide-react";

// ── DocPreviewCard ────────────────────────────────────────────────────────────
// Shows a TL;DR summary (bullets) + formatted preview (intro + first sections)
// for a generated document. Expanded by default so users see the summary
// immediately; can still be collapsed to keep the chat thread clean.
//
// Props:
//   summary: string[]   — up to 5 plain-language bullets (omit/empty → hides bullets)
//   preview: {
//     title:    string,
//     intro:    string,
//     sections: [{ heading: string, snippet: string }],
//     truncated: boolean,
//   } | null
//
// Renders nothing when both summary and preview are empty so callers can pass
// the response straight through without guarding.
// ─────────────────────────────────────────────────────────────────────────────
export default function DocPreviewCard({ summary, preview }) {
  const [expanded, setExpanded] = useState(true);

  const bullets = Array.isArray(summary)
    ? summary.filter((b) => b && String(b).trim())
    : [];
  const previewSections = Array.isArray(preview?.sections)
    ? preview.sections.filter((s) => s && (s.heading || s.snippet))
    : [];
  const intro = (preview?.intro || "").trim();
  const truncated = !!preview?.truncated;

  const hasAnything = bullets.length > 0 || intro || previewSections.length > 0;
  if (!hasAnything) return null;

  return (
    <div className="mt-3 max-w-2xl rounded-xl border border-indigo-100 bg-indigo-50/40 overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="w-full flex items-center justify-between gap-2 px-4 py-2.5
                   text-sm text-indigo-900 hover:bg-indigo-100/60 transition-colors
                   select-none"
      >
        <span className="inline-flex items-center gap-2 font-medium">
          <FileText size={14} className="shrink-0 text-indigo-700" />
          Summary
        </span>
        {expanded ? (
          <ChevronUp size={14} className="shrink-0 text-indigo-700" />
        ) : (
          <ChevronDown size={14} className="shrink-0 text-indigo-700" />
        )}
      </button>

      {expanded && (
        <div className="px-4 pb-4 pt-1 text-sm text-gray-800 space-y-3">
          {bullets.length > 0 && (
            <ul className="list-disc pl-5 space-y-1">
              {bullets.slice(0, 5).map((b, i) => (
                <li key={i} className="leading-snug">{b}</li>
              ))}
            </ul>
          )}

          {intro && (
            <p className="italic text-gray-700 leading-snug">
              {intro}
            </p>
          )}

          {previewSections.length > 0 && (
            <div className="space-y-2">
              {previewSections.map((s, i) => (
                <div
                  key={i}
                  className="rounded-md bg-white/70 border border-indigo-100
                             px-3 py-2"
                >
                  {s.heading && (
                    <div className="text-xs font-semibold text-gray-900 mb-0.5">
                      {s.heading}
                    </div>
                  )}
                  {s.snippet && (
                    <div className="text-xs text-gray-700 leading-snug">
                      {s.snippet}
                    </div>
                  )}
                </div>
              ))}
              {truncated && (
                <div className="text-[11px] text-gray-500 italic">
                  … more sections in the full document
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
