// SPDX-License-Identifier: MIT
// KbDeletionHistory — "Deleted Docs" tab inside Knowledge Base management.
//
// Exports TWO named pieces that KnowledgeBase.jsx slots into its existing
// master-detail shell:
//
//   <KbDeletionList  state={...} />  — compact summary rows; goes inside the
//                                      existing w-72 left rail (below the tab
//                                      strip that's already there).
//   <KbDeletionDetail row={...} />   — full info panel; goes in the right
//                                      flex-1 area next to the left rail.
//
// KnowledgeBase.jsx owns the left-rail wrapper and the tab strip so the
// layout stays consistent with the Chat and Upload tabs — no extra wrapper
// div is introduced here.
//
// Visibility is entirely server-enforced (GET /kb/deleted-history) per the
// ACL rule matrix — super-admin sees everything, HOD sees PUBLIC org-wide
// plus PRIVATE for their own dept, regular user sees only their own rows.

import { useEffect, useState } from "react";
import {
  History, Lock, Globe, Loader2, FileText,
  User, Calendar, CheckCircle2, Trash2,
  ChevronLeft, ChevronRight,
} from "lucide-react";
import { API_BASE, authFetch } from "../config";
import { toIST } from "../utils/time";

const PAGE_SIZE = 25;

function fmtSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024)        return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function fileTypeLabel(row) {
  return (row.original_ext || row.source_type || "").toUpperCase() || "—";
}

// ── Shared state hook — lifted so both panels stay in sync ────────────
// KnowledgeBase.jsx calls useDeletionHistory() once and passes the
// returned state object down to both <KbDeletionList> and <KbDeletionDetail>.
export function useDeletionHistory() {
  const [items, setItems]           = useState([]);
  const [total, setTotal]           = useState(0);
  const [page, setPage]             = useState(1);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState(null);
  const [selectedId, setSelectedId] = useState(null);

  useEffect(() => { _fetch(page); }, [page]);

  async function _fetch(p) {
    setLoading(true);
    setError(null);
    try {
      const res = await authFetch(
        `${API_BASE}/kb/deleted-history?page=${p}&page_size=${PAGE_SIZE}`
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setItems(data.items || []);
      setTotal(data.total || 0);
      setSelectedId(null); // reset detail panel on page change
    } catch {
      setError("Failed to load deletion history");
    } finally {
      setLoading(false);
    }
  }

  const totalPages  = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const selectedRow = items.find(r => r.id === selectedId) || null;

  return {
    items, total, page, setPage,
    loading, error,
    selectedId, setSelectedId,
    totalPages, selectedRow,
  };
}

// ── LEFT: compact summary list ────────────────────────────────────────
// Rendered INSIDE the existing w-72 left rail in KnowledgeBase.jsx,
// below the tab strip. No wrapper div — the rail is owned by the parent.
export function KbDeletionList({ state }) {
  const {
    items, total, page, setPage,
    loading, error,
    selectedId, setSelectedId,
    totalPages,
  } = state;

  return (
    <>
      {/* Doc count sub-header — mirrors the "N docs · N chunks" line in
          the Documents tab so the two tabs feel consistent. */}
      <div className="px-4 py-2 border-b border-gray-100 text-[10px] text-gray-400 flex-shrink-0">
        {total} deleted doc{total !== 1 ? "s" : ""}
      </div>

      {/* Scrollable list */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center h-32 text-gray-400 text-xs gap-2">
            <Loader2 size={14} className="animate-spin" /> Loading…
          </div>
        ) : error ? (
          <div className="p-4 text-xs text-red-500">{error}</div>
        ) : items.length === 0 ? (
          <div className="p-4 text-xs text-gray-400 text-center">
            No deletion history visible to you.
          </div>
        ) : (
          items.map(row => {
            const isSelected = row.id === selectedId;
            return (
              <button
                key={row.id}
                type="button"
                onClick={() => setSelectedId(isSelected ? null : row.id)}
                className={`w-full text-left px-4 py-3 border-b border-gray-100 transition cursor-pointer ${
                  isSelected
                    ? "bg-indigo-50 border-l-2 border-l-indigo-500"
                    : "hover:bg-gray-100"
                }`}
              >
                <div className="flex items-start gap-2">
                  <FileText
                    size={12}
                    className={`mt-0.5 flex-shrink-0 ${isSelected ? "text-indigo-500" : "text-gray-400"}`}
                  />
                  <div className="min-w-0 flex-1">
                    {/* Doc name */}
                    <div
                      className={`text-xs font-medium truncate ${isSelected ? "text-indigo-700" : "text-gray-700"}`}
                      title={row.name}
                    >
                      {row.name}
                    </div>
                    {/* Deleted by */}
                    <div className="text-[10px] text-gray-400 mt-0.5 truncate flex items-center gap-1">
                      <Trash2 size={8} className="flex-shrink-0" />
                      {row.deleted_by || "—"}
                    </div>
                    {/* Deleted at */}
                    <div className="text-[10px] text-gray-400 mt-0.5 flex items-center gap-1">
                      <Calendar size={8} className="flex-shrink-0" />
                      {toIST(row.deleted_at)}
                    </div>
                    {/* Visibility badge */}
                    <div className="mt-1.5">
                      {(row.visibility || "PUBLIC").toUpperCase() === "PRIVATE" ? (
                        <span className="inline-flex items-center gap-0.5 text-[9px] px-1.5 py-0.5 rounded-full bg-amber-50 text-amber-600 border border-amber-200">
                          <Lock size={7} /> Private
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-0.5 text-[9px] px-1.5 py-0.5 rounded-full bg-blue-50 text-blue-500 border border-blue-200">
                          <Globe size={7} /> Public
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </button>
            );
          })
        )}
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between px-4 py-2.5 border-t border-gray-200 bg-white flex-shrink-0 text-[10px] text-gray-400">
          <span>Page {page} of {totalPages}</span>
          <div className="flex gap-1">
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="p-1 rounded border border-gray-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-gray-50 cursor-pointer"
            >
              <ChevronLeft size={11} />
            </button>
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="p-1 rounded border border-gray-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-gray-50 cursor-pointer"
            >
              <ChevronRight size={11} />
            </button>
          </div>
        </div>
      )}
    </>
  );
}

// ── Detail field row ──────────────────────────────────────────────────
function Field({ label, value, mono = false }) {
  if (!value && value !== 0) return null;
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] uppercase tracking-wide text-gray-400 font-medium">{label}</span>
      <span className={`text-xs text-gray-800 break-words ${mono ? "font-mono" : ""}`}>{value}</span>
    </div>
  );
}

// ── RIGHT: detail panel ───────────────────────────────────────────────
// Rendered in the flex-1 right area next to the left rail.
export function KbDeletionDetail({ row }) {
  if (!row) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center text-gray-300 gap-3 select-none">
        <History size={32} strokeWidth={1.2} />
        <span className="text-sm">Select a document to view details</span>
      </div>
    );
  }

  const visPrivate = (row.visibility || "PUBLIC").toUpperCase() === "PRIVATE";

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="px-5 py-4 border-b border-gray-100 flex-shrink-0">
        <div className="flex items-start gap-2">
          <FileText size={15} className="text-indigo-500 mt-0.5 flex-shrink-0" />
          <div className="min-w-0">
            <div className="text-sm font-semibold text-gray-800 leading-snug">{row.name}</div>
            <div className="text-xs text-gray-400 mt-0.5 truncate">{row.filename}</div>
          </div>
        </div>

        {/* Badges */}
        <div className="mt-3 flex items-center gap-2 flex-wrap">
          <span className={`inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-medium border ${
            visPrivate
              ? "bg-amber-50 text-amber-600 border-amber-200"
              : "bg-blue-50 text-blue-600 border-blue-200"
          }`}>
            {visPrivate ? <Lock size={9} /> : <Globe size={9} />}
            {visPrivate ? "Private" : "Public"}
          </span>
          {fileTypeLabel(row) !== "—" && (
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 border border-gray-200 font-medium">
              {fileTypeLabel(row)}
            </span>
          )}
          {row.namespace && (
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-600 border border-indigo-100 font-medium">
              {row.namespace}
            </span>
          )}
        </div>
      </div>

      {/* Scrollable fields */}
      <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-4">

        {/* Deletion event */}
        <div className="rounded-lg border border-red-100 bg-red-50 px-4 py-3 flex flex-col gap-2">
          <div className="flex items-center gap-1.5 text-[10px] font-semibold text-red-500 uppercase tracking-wide">
            <Trash2 size={10} /> Deletion Event
          </div>
          <Field label="Deleted By"  value={row.deleted_by      || "—"} />
          <Field label="Department"  value={row.deleted_by_dept || null} />
          <Field label="Deleted At"  value={toIST(row.deleted_at)} />
        </div>

        {/* Document info */}
        <div className="rounded-lg border border-gray-100 bg-gray-50 px-4 py-3 flex flex-col gap-2">
          <div className="flex items-center gap-1.5 text-[10px] font-semibold text-gray-400 uppercase tracking-wide">
            <FileText size={10} /> Document Info
          </div>
          <Field label="File Size"   value={fmtSize(row.file_size)} />
          <Field label="Chunks"      value={row.chunk_count != null ? `${row.chunk_count} chunks` : null} />
          <Field label="Domain"      value={row.domain       || null} />
          <Field label="Version"     value={row.spec_version || null} />
          <Field label="Source Type" value={row.source_type  || null} />
          {(row.department_ids || []).length > 0 ? (
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] uppercase tracking-wide text-gray-400 font-medium">Departments</span>
              <div className="flex flex-wrap gap-1 mt-0.5">
                {row.department_ids.map(d => (
                  <span key={d} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-600 border border-indigo-100">
                    {d}
                  </span>
                ))}
              </div>
            </div>
          ) : (
            <Field label="Departments" value="All departments" />
          )}
        </div>

        {/* Upload & approval */}
        <div className="rounded-lg border border-gray-100 bg-gray-50 px-4 py-3 flex flex-col gap-2">
          <div className="flex items-center gap-1.5 text-[10px] font-semibold text-gray-400 uppercase tracking-wide">
            <User size={10} /> Upload &amp; Approval
          </div>
          <Field label="Uploaded By"   value={row.uploaded_by      || "—"} />
          <Field label="Uploader Dept" value={row.uploaded_by_dept || null} />
          <Field label="Uploaded At"   value={toIST(row.doc_created_at)} />
          <Field label="Approved By"   value={row.approved_by      || "—"} />
          <Field label="Approved At"   value={toIST(row.approved_at)} />
        </div>

        {/* Reference */}
        <div className="rounded-lg border border-gray-100 bg-gray-50 px-4 py-3 flex flex-col gap-2">
          <div className="flex items-center gap-1.5 text-[10px] font-semibold text-gray-400 uppercase tracking-wide">
            <CheckCircle2 size={10} /> Reference
          </div>
          <Field label="Original Doc ID" value={row.doc_id} mono />
        </div>

      </div>
    </div>
  );
}
