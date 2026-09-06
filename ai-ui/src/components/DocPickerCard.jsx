// SPDX-License-Identifier: MIT
// DocPickerCard — inline multi-select document disambiguation card.
//
// Rendered inside the KbChat message list when the backend returns a
// __clarify__ SSE frame (4+ distinct documents found for the query).
//
// ALL matching documents are shown — no upper cap. If a product has 500
// docs and the query matches 10, all 10 are listed. The list is scrollable
// so the card never grows unbounded in the chat window.
//
// The user selects one or more documents and the original question is
// automatically re-sent scoped to those documents only.
//
// No relevance score/percentage is shown for any document. Chunk-similarity
// scores measure "does this text sound like the question", not "does this
// document actually contain the answer" — those are genuinely different
// things, and displaying a number implies a confidence the system doesn't
// actually have. A document can score highest on wording similarity while
// a lower-scoring document is the one that truly answers the question (a
// real failure mode we hit in testing) — showing "100%" next to the wrong
// pick actively misleads users into trusting it. The list order IS still
// quietly influenced by the backend's relevance ranking (best-guess-first,
// top-2-average per doc — see gateway.py), so a user who has no preference
// still sees the most likely candidates first; nothing is ever displayed
// that implies a confidence level. Users judge by document name/context and
// can freely try a different subset via "Search in selected" if the first
// pick doesn't have the answer, or use "Search in all" to skip the guessing
// entirely.
//
// Props:
//   message     — string: "I found N related documents ... Which would you like?"
//   candidates  — [{doc_id, doc_name}] pre-sorted by the backend, best-guess-first
//   multiSelect — bool: true = checkboxes, false = radio buttons
//   onConfirm   — (selectedDocIds: string[]) => void
//                 Called when user clicks "Search in selected" or "Search in all"

import { useState, useCallback } from "react";
import { FileText, Search, CheckSquare, Square } from "lucide-react";

export default function DocPickerCard({ message, candidates = [], multiSelect = true, onConfirm }) {
  const [selected, setSelected] = useState(new Set());

  const toggle = useCallback((docId) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (multiSelect) {
        if (next.has(docId)) next.delete(docId);
        else next.add(docId);
      } else {
        next.clear();
        next.add(docId);
      }
      return next;
    });
  }, [multiSelect]);

  const handleSelectAll = useCallback(() => {
    if (selected.size === candidates.length) {
      // All selected → deselect all
      setSelected(new Set());
    } else {
      setSelected(new Set(candidates.map(c => c.doc_id)));
    }
  }, [selected, candidates]);

  const handleConfirmSelected = useCallback(() => {
    if (selected.size === 0) return;
    onConfirm?.(Array.from(selected));
  }, [selected, onConfirm]);

  const handleConfirmAll = useCallback(() => {
    onConfirm?.(candidates.map(c => c.doc_id));
  }, [candidates, onConfirm]);

  const allSelected = candidates.length > 0 && selected.size === candidates.length;

  return (
    <div className="flex justify-start my-2">
      <div className="max-w-xl w-full rounded-xl border border-blue-200 bg-blue-50/60 shadow-sm p-4 space-y-3">

        {/* Header */}
        <div className="flex items-start gap-2">
          <FileText size={16} className="text-blue-500 mt-0.5 shrink-0" />
          <p className="text-sm text-gray-700 leading-snug">{message}</p>
        </div>

        {/* Select-all toggle (only shown for multi-select with 2+ docs) */}
        {multiSelect && candidates.length > 1 && (
          <button
            onClick={handleSelectAll}
            className="flex items-center gap-1.5 text-xs text-blue-600 hover:text-blue-800 font-medium transition-colors"
          >
            {allSelected
              ? <CheckSquare size={13} />
              : <Square size={13} />
            }
            {allSelected ? "Deselect all" : `Select all (${candidates.length})`}
          </button>
        )}

        {/* Document list — scrollable when many docs */}
        <div className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
          {candidates.map((c) => {
            const isChecked = selected.has(c.doc_id);
            return (
              <label
                key={c.doc_id}
                className={`flex items-center gap-3 rounded-lg px-3 py-2 cursor-pointer transition-colors
                  ${isChecked
                    ? "bg-blue-100 border border-blue-300"
                    : "bg-white border border-gray-200 hover:bg-gray-50"
                  }`}
              >
                <input
                  type={multiSelect ? "checkbox" : "radio"}
                  name="doc-picker"
                  checked={isChecked}
                  onChange={() => toggle(c.doc_id)}
                  className="accent-blue-500 shrink-0"
                />
                <span className="flex-1 text-sm text-gray-800 truncate" title={c.doc_name}>
                  {c.doc_name}
                </span>
              </label>
            );
          })}
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={handleConfirmSelected}
            disabled={selected.size === 0}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors
              ${selected.size > 0
                ? "bg-blue-600 text-white hover:bg-blue-700"
                : "bg-gray-200 text-gray-400 cursor-not-allowed"
              }`}
          >
            <Search size={13} />
            Search in selected{selected.size > 0 ? ` (${selected.size})` : ""}
          </button>

          <button
            onClick={handleConfirmAll}
            className="px-3 py-1.5 rounded-lg text-sm font-medium border border-gray-300 bg-white text-gray-700 hover:bg-gray-50 transition-colors"
          >
            Search in all ({candidates.length})
          </button>
        </div>
      </div>
    </div>
  );
}
