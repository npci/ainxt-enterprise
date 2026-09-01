// SPDX-License-Identifier: Apache-2.0
// DiffApprovalPanel — "decide before the gate" shift-left.
// Renders the REAL, already-compiled + already-tested diff (the VERIFIED_DIFF
// artifact) at AWAITING_CODE_APPROVAL (legacy: AWAITING_DESIGN_APPROVAL) /
// AWAITING_SOLUTION_APPROVAL so the human approves concrete changes — not a JSON
// plan. Shows per-file SEARCH/REPLACE (or new-file) bodies + compile/test status
// badges + any compile-waiver banner.
//
// Per-file request-changes comments (2026-07-29): an optional comment textbox
// (+ optional line number) per file, shown at the pre-apply code-approval gate
// AND the PR-approval gate (AWAITING_PR_APPROVAL) — see backend contract on
// POST /runs/{id}/request-changes `file_comments`. Non-empty entries are bubbled
// up to the parent (the HITL ApprovalPanel in SDLCPipeline.jsx, which owns the
// whole-run feedback textarea + the actual POST) via `onFileCommentsChange`.
import { useState, useEffect, useRef } from "react";
import { ChevronDown, ChevronRight, ChevronUp, Copy, Check } from "lucide-react";
import { API_BASE as API, apiFetch } from "../config";

function StatusBadge({ ok, skipped, label }) {
  if (skipped) {
    return <span className="text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full">{label}: waived</span>;
  }
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full ${ok ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>
      {label}: {ok ? "✓ passed" : "✗ failed"}
    </span>
  );
}

// Line-level LCS diff between old and new file bodies. Returns a flat list of
// ops: {type: "equal"|"del"|"add", a, b, ai, bj}. Pure JS, no deps — the
// VERIFIED_DIFF payload already ships both base_body and new_body per edit.
const MAX_DIFF_LINES = 4000; // guard: skip O(n*m) LCS on very large files
const VIRTUAL_THRESHOLD = 500; // rows above which virtual rendering is used
const DIFF_ROW_HEIGHT = 20;    // px — matches text-xs font-mono line height
const DIFF_CONTAINER_HEIGHT = 600; // px — replaces max-h-96 (384px)

function diffLines(oldStr, newStr) {
  const a = oldStr ? oldStr.split("\n") : [];
  const b = newStr ? newStr.split("\n") : [];
  const n = a.length;
  const m = b.length;
  // Too large to diff cheaply — degrade to a whole-file replace view.
  if (n > MAX_DIFF_LINES || m > MAX_DIFF_LINES) {
    const ops = [];
    for (let i = 0; i < n; i++) ops.push({ type: "del", a: a[i], ai: i });
    for (let j = 0; j < m; j++) ops.push({ type: "add", b: b[j], bj: j });
    return { ops, truncated: true };
  }
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push({ type: "equal", a: a[i], b: b[j], ai: i, bj: j }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ type: "del", a: a[i], ai: i }); i++; }
    else { ops.push({ type: "add", b: b[j], bj: j }); j++; }
  }
  while (i < n) { ops.push({ type: "del", a: a[i], ai: i }); i++; }
  while (j < m) { ops.push({ type: "add", b: b[j], bj: j }); j++; }
  return { ops, truncated: false };
}

// Pair consecutive del/add blocks into aligned rows for the side-by-side view.
function toSplitRows(ops) {
  const rows = [];
  let k = 0;
  while (k < ops.length) {
    const op = ops[k];
    if (op.type === "equal") {
      rows.push({ type: "equal", left: op.a, ln: op.ai + 1, right: op.b, rn: op.bj + 1 });
      k++;
      continue;
    }
    const dels = [];
    const adds = [];
    while (k < ops.length && ops[k].type === "del") { dels.push(ops[k]); k++; }
    while (k < ops.length && ops[k].type === "add") { adds.push(ops[k]); k++; }
    const max = Math.max(dels.length, adds.length);
    for (let x = 0; x < max; x++) {
      const d = dels[x];
      const ad = adds[x];
      rows.push({
        type: d && ad ? "change" : (d ? "del" : "add"),
        left: d ? d.a : null,
        ln: d ? d.ai + 1 : null,
        right: ad ? ad.b : null,
        rn: ad ? ad.bj + 1 : null,
      });
    }
  }
  return rows;
}

// Group a rendered item list (unified ops or split rows) into hunks — maximal
// runs of changed lines. Returns the item-index of each hunk's first changed row
// so the in-file navigator can jump straight to the next/prev change instead of
// forcing the reviewer to scroll the whole file.
function hunkStartIndices(items) {
  const starts = [];
  let inHunk = false;
  items.forEach((it, idx) => {
    const changed = it.type !== "equal";
    if (changed && !inHunk) { starts.push(idx); inHunk = true; }
    else if (!changed) { inHunk = false; }
  });
  return starts;
}

function VirtualList({ items, renderRow, rowHeight, containerHeight }) {
  const outerRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const visibleCount = Math.ceil(containerHeight / rowHeight);
  const overscan = 10;
  const startIdx = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const endIdx = Math.min(items.length - 1, startIdx + visibleCount + overscan * 2);
  const totalHeight = items.length * rowHeight;
  return (
    <div
      ref={outerRef}
      style={{ height: containerHeight, overflowY: "auto", position: "relative" }}
      className="mt-1 text-xs font-mono bg-white border border-gray-200 rounded"
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
    >
      <div style={{ height: totalHeight, position: "relative" }}>
        {items.slice(startIdx, endIdx + 1).map((item, localIdx) => {
          const absoluteIdx = startIdx + localIdx;
          return (
            <div
              key={absoluteIdx}
              style={{ position: "absolute", top: absoluteIdx * rowHeight, width: "100%", height: rowHeight }}
            >
              {renderRow(item, absoluteIdx)}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function UnifiedDiff({ ops, containerRef, rowRef }) {
  if (ops.length > VIRTUAL_THRESHOLD) {
    return (
      <VirtualList
        items={ops}
        rowHeight={DIFF_ROW_HEIGHT}
        containerHeight={DIFF_CONTAINER_HEIGHT}
        renderRow={(op) => {
          const cls = op.type === "add"
            ? "bg-green-50 text-green-800"
            : op.type === "del"
            ? "bg-red-50 text-red-800"
            : "text-gray-600";
          const sign = op.type === "add" ? "+" : op.type === "del" ? "-" : " ";
          const oldNo = op.type === "add" ? "" : (op.ai + 1);
          const newNo = op.type === "del" ? "" : (op.bj + 1);
          const text = op.type === "del" ? op.a : op.b;
          return (
            <div className={`flex whitespace-pre ${cls}`} style={{ height: DIFF_ROW_HEIGHT }}>
              <span className="w-10 shrink-0 text-right pr-2 text-gray-400 select-none">{oldNo}</span>
              <span className="w-10 shrink-0 text-right pr-2 text-gray-400 select-none">{newNo}</span>
              <span className="w-4 shrink-0 select-none">{sign}</span>
              <span className="flex-1">{text === "" ? " " : text}</span>
            </div>
          );
        }}
      />
    );
  }
  return (
    <div ref={containerRef} className="relative mt-1 text-xs font-mono bg-white border border-gray-200 rounded overflow-auto max-h-[600px]">
      {ops.map((op, idx) => {
        const cls = op.type === "add"
          ? "bg-green-50 text-green-800"
          : op.type === "del"
          ? "bg-red-50 text-red-800"
          : "text-gray-600";
        const sign = op.type === "add" ? "+" : op.type === "del" ? "-" : " ";
        const oldNo = op.type === "add" ? "" : (op.ai + 1);
        const newNo = op.type === "del" ? "" : (op.bj + 1);
        const text = op.type === "del" ? op.a : op.b;
        return (
          <div key={idx} ref={(el) => rowRef && rowRef(idx, el)} className={`flex whitespace-pre ${cls}`}>
            <span className="w-10 shrink-0 text-right pr-2 text-gray-400 select-none">{oldNo}</span>
            <span className="w-10 shrink-0 text-right pr-2 text-gray-400 select-none">{newNo}</span>
            <span className="w-4 shrink-0 select-none">{sign}</span>
            <span className="flex-1">{text === "" ? " " : text}</span>
          </div>
        );
      })}
    </div>
  );
}

function SplitDiff({ ops, containerRef, rowRef }) {
  const rows = toSplitRows(ops);
  if (rows.length > VIRTUAL_THRESHOLD) {
    return (
      <VirtualList
        items={rows}
        rowHeight={DIFF_ROW_HEIGHT}
        containerHeight={DIFF_CONTAINER_HEIGHT}
        renderRow={(row) => {
          const leftCls = row.type === "del" || row.type === "change" ? "bg-red-50 text-red-800" : "text-gray-600";
          const rightCls = row.type === "add" || row.type === "change" ? "bg-green-50 text-green-800" : "text-gray-600";
          return (
            <div className="flex whitespace-pre" style={{ height: DIFF_ROW_HEIGHT }}>
              <span className="w-10 shrink-0 text-right pr-2 text-gray-400 select-none border-r border-gray-100">{row.ln ?? ""}</span>
              <span className={`w-1/2 shrink-0 px-2 border-r border-gray-200 ${leftCls}`}>{row.left == null ? " " : (row.left === "" ? " " : row.left)}</span>
              <span className="w-10 shrink-0 text-right pr-2 text-gray-400 select-none border-r border-gray-100">{row.rn ?? ""}</span>
              <span className={`flex-1 px-2 ${rightCls}`}>{row.right == null ? " " : (row.right === "" ? " " : row.right)}</span>
            </div>
          );
        }}
      />
    );
  }
  return (
    <div ref={containerRef} className="relative mt-1 text-xs font-mono bg-white border border-gray-200 rounded overflow-auto max-h-[600px]">
      {rows.map((row, idx) => {
        const leftCls = row.type === "del" || row.type === "change" ? "bg-red-50 text-red-800" : "text-gray-600";
        const rightCls = row.type === "add" || row.type === "change" ? "bg-green-50 text-green-800" : "text-gray-600";
        return (
          <div key={idx} ref={(el) => rowRef && rowRef(idx, el)} className="flex items-start whitespace-pre">
            <span className="w-10 shrink-0 text-right pr-2 text-gray-400 select-none border-r border-gray-100">{row.ln ?? ""}</span>
            <span className={`w-1/2 shrink-0 px-2 border-r border-gray-200 whitespace-pre-wrap break-words ${leftCls}`}>{row.left == null ? " " : (row.left === "" ? " " : row.left)}</span>
            <span className="w-10 shrink-0 text-right pr-2 text-gray-400 select-none border-r border-gray-100">{row.rn ?? ""}</span>
            <span className={`flex-1 px-2 whitespace-pre-wrap break-words ${rightCls}`}>{row.right == null ? " " : (row.right === "" ? " " : row.right)}</span>
          </div>
        );
      })}
    </div>
  );
}

function FileDiff({ edit, canComment, comment, onCommentChange, open: openProp, onToggleOpen, fileRef }) {
  // `open` is controlled by the parent when onToggleOpen is provided (primary
  // edits list — so the file-level Prev/Next navigator can auto-expand a target
  // file); otherwise the card self-manages (dependent-repo group).
  const controlled = typeof onToggleOpen === "function";
  const [openInternal, setOpenInternal] = useState(false);
  const open = controlled ? !!openProp : openInternal;
  const toggleOpen = () => (controlled ? onToggleOpen() : setOpenInternal(o => !o));
  const [copied, setCopied] = useState(false);
  const [mode, setMode] = useState("unified"); // "unified" | "split"
  // In-file change navigator: the diff container owns the scroll and each row
  // registers its DOM node so Prev/Next scrolls a hunk's first changed row to the
  // top of the container rather than making the reviewer scroll line-by-line.
  const diffContainerRef = useRef(null);
  const rowNodesRef = useRef([]);
  const [activeHunk, setActiveHunk] = useState(0);
  const path = edit.path || "";
  const isNew = !!edit.is_new;
  const deleted = !!edit.deleted;
  const kind = edit.kind === "slt" ? "SLT" : (edit.is_test ? "TEST" : "CODE");
  const oldBody = edit.base_body || "";
  const newBody = edit.new_body || "";
  const hasContent = !!(oldBody || newBody);

  const { ops, truncated } = diffLines(oldBody, newBody);
  const added = ops.filter(o => o.type === "add").length;
  const removed = ops.filter(o => o.type === "del").length;

  // Hunk anchors for the currently rendered view (split rows vs unified ops).
  const hunkItems = mode === "split" ? toSplitRows(ops) : ops;
  const hunkStarts = hunkStartIndices(hunkItems);
  useEffect(() => { setActiveHunk(0); rowNodesRef.current = []; }, [mode, open]);
  const registerRow = (idx, el) => { rowNodesRef.current[idx] = el; };
  const goToHunk = (h) => {
    if (!hunkStarts.length) return;
    let n = h;
    if (n < 0) n = hunkStarts.length - 1;
    if (n >= hunkStarts.length) n = 0;
    const node = rowNodesRef.current[hunkStarts[n]];
    const cont = diffContainerRef.current;
    if (node && cont) cont.scrollTop = Math.max(0, node.offsetTop - 8);
    setActiveHunk(n);
  };

  const changeLabel = isNew ? "CREATE" : deleted ? "DELETE" : "MODIFY";
  const changeCls = isNew
    ? "bg-green-100 text-green-700"
    : deleted
    ? "bg-red-100 text-red-700"
    : "bg-blue-100 text-blue-700";

  const copy = () => {
    navigator.clipboard.writeText(path);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div ref={fileRef} className="border border-gray-200 rounded-lg p-3 bg-white scroll-mt-20">
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`px-2 py-0.5 rounded text-xs font-medium ${changeCls}`}>
          {changeLabel}
        </span>
        <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-500">{kind}</span>
        <code className="text-xs text-gray-700 font-mono">{path}</code>
        <button onClick={copy} className="text-gray-400 hover:text-gray-700">
          {copied ? <Check size={12} /> : <Copy size={12} />}
        </button>
        {(added > 0 || removed > 0) && (
          <span className="text-[10px] font-mono ml-auto">
            <span className="text-green-600">+{added}</span>{" "}
            <span className="text-red-600">-{removed}</span>
          </span>
        )}
      </div>
      {hasContent && (
        <div className="mt-2">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <button
              className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800"
              onClick={toggleOpen}
            >
              {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              {open ? "Hide" : "Show"} diff
            </button>
            {open && (
              <div className="flex items-center rounded border border-gray-200 overflow-hidden text-[10px]">
                <button
                  className={`px-2 py-0.5 ${mode === "unified" ? "bg-indigo-600 text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}
                  onClick={() => setMode("unified")}
                >
                  Unified
                </button>
                <button
                  className={`px-2 py-0.5 border-l border-gray-200 ${mode === "split" ? "bg-indigo-600 text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}
                  onClick={() => setMode("split")}
                >
                  Split
                </button>
              </div>
            )}
          </div>
          {open && truncated && (
            <div className="mt-1 text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
              File too large for a line-by-line diff — showing full removed/added content.
            </div>
          )}
          {open && hunkStarts.length > 1 && (
            <div className="flex items-center gap-1 mt-1 text-[10px] text-gray-500">
              <span className="mr-1">{hunkStarts.length} changes in this file</span>
              <button
                type="button"
                className="px-1.5 py-0.5 rounded border border-gray-200 hover:bg-gray-50 flex items-center gap-0.5"
                onClick={() => goToHunk(activeHunk - 1)}
                title="Previous change in this file"
              >
                <ChevronUp size={11} /> Prev
              </button>
              <button
                type="button"
                className="px-1.5 py-0.5 rounded border border-gray-200 hover:bg-gray-50 flex items-center gap-0.5"
                onClick={() => goToHunk(activeHunk + 1)}
                title="Next change in this file"
              >
                <ChevronDown size={11} /> Next
              </button>
              <span className="font-mono">{Math.min(activeHunk + 1, hunkStarts.length)}/{hunkStarts.length}</span>
            </div>
          )}
          {open && (mode === "split"
            ? <SplitDiff ops={ops} containerRef={diffContainerRef} rowRef={registerRow} />
            : <UnifiedDiff ops={ops} containerRef={diffContainerRef} rowRef={registerRow} />)}
        </div>
      )}
      {canComment && (
        <div className="mt-2 pt-2 border-t border-gray-100 flex items-center gap-2">
          <input
            type="number"
            placeholder="line"
            title="Optional line number for this comment"
            className="w-14 border border-gray-200 rounded px-1.5 py-1 text-xs text-gray-700 focus:outline-none focus:ring-1 focus:ring-indigo-300"
            value={comment?.line ?? ""}
            onChange={e => onCommentChange?.({ line: e.target.value === "" ? null : Number(e.target.value) })}
          />
          <input
            type="text"
            placeholder="Optional comment on this file…"
            className="flex-1 border border-gray-200 rounded px-2 py-1 text-xs text-gray-700 focus:outline-none focus:ring-1 focus:ring-indigo-300"
            value={comment?.comment ?? ""}
            onChange={e => onCommentChange?.({ comment: e.target.value })}
          />
        </div>
      )}
    </div>
  );
}

// Dependent-repo edits: staged in the run workspace but pushed as a SEPARATE
// sibling merge request against the dep's own GitLab repo — never applied to
// the primary repo. These historically bypassed both the Opus REVIEW gate and
// this HITL gate, so they're rendered here, visually distinct, informational
// only (no approve/reject control — approval remains one decision per run).
function DepRepoGroup({ repoKey, group }) {
  const [open, setOpen] = useState(true);
  const edits = Array.isArray(group?.edits) ? group.edits : [];
  const repo = group?.repo || repoKey;
  return (
    <div className="border-2 border-amber-300 rounded-lg p-3 bg-amber-50">
      <button
        className="flex items-center gap-2 flex-wrap w-full text-left"
        onClick={() => setOpen(o => !o)}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <code className="text-xs font-mono font-semibold text-amber-900">{repo}</code>
        <span className="text-[10px] font-semibold uppercase tracking-wide bg-amber-200 text-amber-900 px-2 py-0.5 rounded-full">
          separate sibling merge request
        </span>
        <span className="text-xs text-gray-500 ml-auto">{edits.length} file(s)</span>
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          {edits.map((e, i) => (
            <FileDiff key={i} edit={e} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function DiffApprovalPanel({ run, onFileCommentsChange }) {
  const runId = run?.id;
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  // Per-file request-changes comments, keyed by file path -> { line, comment }.
  const [fileComments, setFileComments] = useState({});
  // File-level change navigation: which file cards are expanded, the currently
  // focused file (for the "Change X of N" counter + Prev/Next), and DOM refs to
  // each card so navigation can scroll it into view.
  const [openIdx, setOpenIdx] = useState(() => new Set());
  const [activeIdx, setActiveIdx] = useState(0);
  const fileRefs = useRef([]);

  // Per-file comments are available at both the pre-apply code-approval gate
  // (AWAITING_CODE_APPROVAL, legacy AWAITING_DESIGN_APPROVAL) and the PR-approval
  // gate (AWAITING_PR_APPROVAL) — see CLAUDE.md backend contract note on
  // POST /runs/{id}/request-changes `file_comments`.
  const canComment = run?.state === "AWAITING_CODE_APPROVAL"
      || run?.state === "AWAITING_DESIGN_APPROVAL"
      || run?.state === "AWAITING_PR_APPROVAL";

  useEffect(() => {
    if (typeof onFileCommentsChange !== "function") return;
    const collected = Object.entries(fileComments)
      .filter(([, v]) => v && typeof v.comment === "string" && v.comment.trim().length > 0)
      .map(([file, v]) => ({ file, line: v.line ?? null, comment: v.comment.trim() }));
    onFileCommentsChange(collected);
  }, [fileComments, onFileCommentsChange]);

  useEffect(() => {
    if (!runId) return;
    setLoading(true);
    apiFetch(`${API}/sdlc/runs/${runId}/verified-diff`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, [runId, run?.state]);

  // ── file-level change navigation (Prev/Next changed file + j/k keys) ───────
  // Derived before the early returns so the keyboard effect can depend on the
  // edit count without violating the rules of hooks. Empty until the diff loads.
  const editsNav = ((data && data.verified_diff) || {}).edits || [];
  const allOpen = editsNav.length > 0 && openIdx.size >= editsNav.length;
  const toggleAll = () =>
    setOpenIdx(allOpen ? new Set() : new Set(editsNav.map((_, i) => i)));
  const goToFile = (idx) => {
    if (!editsNav.length) return;
    let n = idx;
    if (n < 0) n = editsNav.length - 1;
    if (n >= editsNav.length) n = 0;
    setActiveIdx(n);
    setOpenIdx(prev => { const s = new Set(prev); s.add(n); return s; });
    requestAnimationFrame(() => {
      const el = fileRefs.current[n];
      if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  };
  useEffect(() => {
    const onKey = (e) => {
      const tag = (e.target && e.target.tagName ? e.target.tagName : "").toLowerCase();
      if (tag === "input" || tag === "textarea" || (e.target && e.target.isContentEditable)) return;
      if (!editsNav.length) return;
      if (e.key === "j" || e.key === "n") { e.preventDefault(); goToFile(activeIdx + 1); }
      else if (e.key === "k" || e.key === "p") { e.preventDefault(); goToFile(activeIdx - 1); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editsNav.length, activeIdx]);

  if (loading) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
        <p className="text-sm text-gray-500">Building the diff (compiling &amp; running tests)…</p>
      </div>
    );
  }
  if (!data || !data.verified_diff) return null;

  const vd = data.verified_diff || {};
  const edits = vd.edits || [];
  const compile = vd.compile || {};
  const tests = vd.tests || {};
  const banners = data.waiver_banners || [];
  // Request-Changes must show the model the REJECTED diff on the next pre-gate
  // pass — surfaced here so the reviewer sees the prior staleness re-gate reason.
  const regateReason = (run?.context || {}).applying_regate_reason;
  // Additive/optional — omitted entirely for single-repo runs (the common case).
  const depEditsByRepo = vd.dep_edits_by_repo || {};
  const depRepoKeys = Object.keys(depEditsByRepo || {});

  if (edits.length === 0 && depRepoKeys.length === 0) return null;

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <span className="text-lg">🔍</span>
        <h3 className="font-semibold text-gray-800">Verified Diff — Approve the Real Change</h3>
        <StatusBadge ok={compile.passed} skipped={compile.skipped} label="compile" />
        <StatusBadge ok={tests.passed} skipped={tests.skipped} label="tests" />
      </div>
      <p className="text-xs text-gray-500 mb-2">
        {edits.length} file(s) — already compiled and tested before this gate.
        {vd.base_sha && <span className="font-mono"> base {String(vd.base_sha).slice(0, 8)}</span>}
      </p>

      {banners.length > 0 && (
        <div className="mb-3 space-y-1">
          {banners.map((b, i) => (
            <div key={i} className="text-xs bg-amber-50 border border-amber-200 text-amber-800 rounded px-2 py-1">{b}</div>
          ))}
        </div>
      )}
      {regateReason && (
        <div className="mb-3 text-xs bg-red-50 border border-red-200 text-red-800 rounded px-2 py-1">
          Re-review required: {regateReason}
        </div>
      )}

      {edits.length > 1 && (
        <div className="sticky top-0 z-10 -mx-4 px-4 py-2 mb-2 bg-white/95 backdrop-blur border-b border-gray-100 flex items-center gap-2 flex-wrap">
          <span className="text-xs text-gray-600">
            Change {Math.min(activeIdx + 1, edits.length)} of {edits.length}
          </span>
          <div className="flex items-center rounded border border-gray-200 overflow-hidden">
            <button
              type="button"
              className="px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 flex items-center gap-1"
              onClick={() => goToFile(activeIdx - 1)}
              title="Previous changed file (k / p)"
            >
              <ChevronUp size={12} /> Prev
            </button>
            <button
              type="button"
              className="px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 border-l border-gray-200 flex items-center gap-1"
              onClick={() => goToFile(activeIdx + 1)}
              title="Next changed file (j / n)"
            >
              <ChevronDown size={12} /> Next
            </button>
          </div>
          <button
            type="button"
            className="px-2 py-1 text-xs text-indigo-600 hover:text-indigo-800 ml-auto"
            onClick={toggleAll}
          >
            {allOpen ? "Collapse all" : "Expand all"}
          </button>
          <span className="text-[10px] text-gray-400 hidden sm:inline">press j / k to jump</span>
        </div>
      )}

      {edits.length > 0 && (
        <div className="space-y-2">
          {edits.map((e, i) => {
            const path = e.path || "";
            return (
              <FileDiff
                key={i}
                edit={e}
                canComment={canComment}
                comment={fileComments[path]}
                onCommentChange={(patch) => setFileComments(m => ({ ...m, [path]: { ...m[path], ...patch } }))}
                open={openIdx.has(i)}
                onToggleOpen={() => {
                  setActiveIdx(i);
                  setOpenIdx(prev => {
                    const s = new Set(prev);
                    if (s.has(i)) s.delete(i); else s.add(i);
                    return s;
                  });
                }}
                fileRef={(el) => { fileRefs.current[i] = el; }}
              />
            );
          })}
        </div>
      )}

      {depRepoKeys.length > 0 && (
        <div className="mt-4 pt-3 border-t border-gray-200">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-lg">🔗</span>
            <h4 className="font-semibold text-sm text-gray-800">Dependent-Repo Changes — Sibling Merge Requests</h4>
          </div>
          <p className="text-xs text-gray-500 mb-2">
            These edits target a different repo than this run's primary repo. They are pushed as their own merge
            request(s) and are shown here for review context only — this gate's decision does not apply/reject them individually.
          </p>
          <div className="space-y-2">
            {depRepoKeys.map((repoKey) => (
              <DepRepoGroup key={repoKey} repoKey={repoKey} group={depEditsByRepo[repoKey]} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
