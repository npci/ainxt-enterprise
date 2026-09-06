// SPDX-License-Identifier: MIT
/* FileEditorPanel — lite-IDE right pane for the Code tab.
 *
 * Tabbed, editable file viewer (CodeMirror 6). Each open file has its own buffer
 * with dirty tracking; Save (or Cmd/Ctrl+S) writes back to disk via the guarded
 * write-file IPC. When the agent edits a file, the parent flips that tab to the
 * "Diff" view (red/green) using the shared DiffLines renderer.
 *
 * Paths are "/"-relative to the workspace root; `absOf(rel)` (from the parent)
 * converts to the OS-absolute path for readFile/writeFile.
 */
import { useEffect, useRef, useState, useCallback } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { githubLight, githubDark } from "@uiw/codemirror-theme-github";
import { languages } from "@codemirror/language-data";
import { unifiedMergeView } from "@codemirror/merge";
import { EditorView } from "@codemirror/view";

// Brighter, more legible diff highlight than CodeMirror-merge's pale defaults.
// Line backgrounds are tinted; the actual changed text is tinted stronger.
const diffHighlightTheme = EditorView.theme({
  ".cm-changedLine": { backgroundColor: "rgba(34,197,94,0.16)" },
  ".cm-changedText": { backgroundColor: "rgba(34,197,94,0.42)", borderRadius: "2px" },
  ".cm-changedLineGutter": { backgroundColor: "rgba(34,197,94,0.20)" },
  ".cm-deletedChunk": { backgroundColor: "rgba(244,63,94,0.12)" },
  ".cm-deletedChunk .cm-deletedText, .cm-deletedText": { backgroundColor: "rgba(244,63,94,0.40)", borderRadius: "2px" },
  ".cm-deletedLine": { backgroundColor: "rgba(244,63,94,0.16)" },
});
import { X, Save, RotateCcw, PanelRightClose, FileText, FileDiff, Loader2, AlertCircle, Check, Ban, Eye } from "lucide-react";
import { readFile, writeFile } from "../../hooks/useDesktop.js";
import DiffLines from "./DiffLines.jsx";

const baseName = (rel) => rel.split("/").pop();

// Reconstruct the proposed full-file content from the original + the agent's
// hunk, so we can show a full-file inline diff BEFORE the edit is applied to
// disk. Edit/MultiEdit blocks are "-" (old) then "+" (new), split by "@@";
// Write carries the whole new content as "+" lines. Returns null when it can't
// be applied cleanly (truncated hunk, old text not found) → caller falls back.
function reconstructAfter(before, entry) {
  if (!entry || entry.truncated) return null;
  const lines = entry.lines || [];
  if (entry.isNew) return lines.filter((l) => l.kind === "+").map((l) => l.line).join("\n");
  const blocks = []; let cur = { old: [], neu: [] };
  for (const l of lines) {
    if (l.kind === "@@") { blocks.push(cur); cur = { old: [], neu: [] }; continue; }
    if (l.kind === "-") cur.old.push(l.line);
    else if (l.kind === "+") cur.neu.push(l.line);
  }
  blocks.push(cur);
  let after = before;
  for (const b of blocks) {
    const oldStr = b.old.join("\n"), newStr = b.neu.join("\n");
    if (!oldStr) return null;
    if (!after.includes(oldStr)) return null;
    after = after.replace(oldStr, newStr);
  }
  return after;
}

// Lazily resolve a CodeMirror language extension by file extension.
async function loadLangExtension(rel) {
  const ext = (rel.split(".").pop() || "").toLowerCase();
  const desc = languages.find((l) => l.extensions?.includes(ext));
  if (!desc) return null;
  try { return await desc.load(); } catch { return null; }
}

export default function FileEditorPanel({
  openFiles = [], activeFile, mode = "edit", changedFiles,
  absOf, onSelectTab, onCloseTab, onSetMode, onClose, onSaved, onDirtyChange,
  reloadSignal = 0, reloadTarget = null, showTabs = true,
  pendingConfirm = null, pendingRel = null, onAnswer, dark = false,
}) {
  const cmTheme = dark ? githubDark : githubLight;
  // bufs[rel] = { content, saved, dirty, loading, error }
  const [bufs, setBufs] = useState({});
  const [langExt, setLangExt] = useState(null);
  const [saving, setSaving] = useState(false);
  const bufsRef = useRef(bufs);
  useEffect(() => { bufsRef.current = bufs; }, [bufs]);
  const loadingRef = useRef(new Set()); // rels with an in-flight readFile

  const changed = changedFiles || new Map();
  const buf = activeFile ? bufs[activeFile] : null;
  const entry = activeFile ? changed.get(activeFile) : null;
  const hasDiff = !!entry;
  // HTML/SVG files can be rendered live in a sandboxed iframe — e.g. anything
  // the agent generates as a self-contained page.
  const isPreviewable = /\.(html?|svg)$/i.test(activeFile || "");
  const effectiveMode = mode === "preview" && isPreviewable ? "preview"
    : mode === "diff" && hasDiff ? "diff" : "edit";
  // Proposed full-file result (original + applied hunk) for the inline diff.
  const before = entry?.before;
  const after = before != null ? reconstructAfter(before, entry) : null;
  // This file has a pending edit awaiting the user's approval.
  const isPendingHere = !!pendingConfirm && pendingRel && pendingRel === activeFile;

  // Load file content the first time a tab becomes active (state set only in the
  // async callback — until it resolves, `!buf` renders the loading state).
  useEffect(() => {
    if (!activeFile) return;
    if (bufsRef.current[activeFile] || loadingRef.current.has(activeFile)) return;
    let cancelled = false;
    loadingRef.current.add(activeFile);
    readFile(absOf(activeFile)).then((res) => {
      loadingRef.current.delete(activeFile);
      if (cancelled) return;
      const content = res?.error ? "" : (res?.content ?? "");
      setBufs((b) => ({ ...b, [activeFile]: { content, saved: content, dirty: false, error: res?.error || null } }));
    });
    return () => { cancelled = true; };
  }, [activeFile, absOf]);

  // Resolve syntax highlighting for the active file (set only in async callback).
  useEffect(() => {
    if (!activeFile) return;
    let cancelled = false;
    loadLangExtension(activeFile).then((ext) => { if (!cancelled) setLangExt(ext); });
    return () => { cancelled = true; };
  }, [activeFile]);

  const setContent = useCallback((rel, content) => {
    setBufs((b) => {
      const prev = b[rel]; if (!prev) return b;
      return { ...b, [rel]: { ...prev, content, dirty: content !== prev.saved } };
    });
  }, []);

  const reload = useCallback((rel) => {
    if (!rel) return;
    readFile(absOf(rel)).then((res) => {
      if (res?.error) return;
      const content = res?.content ?? "";
      setBufs((b) => ({ ...b, [rel]: { content, saved: content, dirty: false, loading: false, error: null } }));
    });
  }, [absOf]);

  const save = useCallback(async (rel) => {
    const target = rel || activeFile;
    const b = bufsRef.current[target];
    if (!target || !b || !b.dirty || saving) return;
    setSaving(true);
    const res = await writeFile(absOf(target), b.content);
    setSaving(false);
    if (res?.ok) {
      setBufs((bb) => ({ ...bb, [target]: { ...bb[target], saved: b.content, dirty: false } }));
      onSaved?.(target);
    }
  }, [activeFile, absOf, saving, onSaved]);

  useEffect(() => { onDirtyChange?.(activeFile, !!buf?.dirty); }, [activeFile, buf?.dirty, onDirtyChange]);

  // Watcher (parent) bumps reloadSignal for a file that changed on disk — refresh
  // its buffer, but never clobber unsaved edits.
  useEffect(() => {
    if (!reloadSignal || !reloadTarget) return;
    const b = bufsRef.current[reloadTarget];
    if (b && !b.dirty) reload(reloadTarget);
  }, [reloadSignal, reloadTarget, reload]);

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "s" || e.key === "S")) {
      e.preventDefault(); e.stopPropagation(); save();
    }
  };

  if (!activeFile) {
    return (
      <div className="h-full flex flex-col items-center justify-center text-center px-6 text-gray-400">
        <FileText className="w-8 h-8 mb-2" />
        <p className="text-sm">Open a file from the explorer</p>
        <p className="text-xs mt-1">or the agent will open files here as it edits them.</p>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col min-h-0 min-w-0 overflow-hidden bg-white" onKeyDown={onKeyDown}>
      {/* Tab strip (hidden when the parent provides its own tab bar) */}
      {showTabs && (
      <div className="flex items-stretch border-b border-gray-200 bg-gray-50 shrink-0 overflow-x-auto">
        {openFiles.map((rel) => {
          const active = rel === activeFile;
          const dirty = bufs[rel]?.dirty;
          return (
            <div key={rel} onClick={() => onSelectTab?.(rel)} title={rel}
              className={`group flex items-center gap-1.5 px-3 py-1.5 text-xs border-r border-gray-200 cursor-pointer max-w-[12rem] ${active ? "bg-white text-gray-800" : "text-gray-500 hover:bg-gray-100"}`}>
              <FileText className={`w-3.5 h-3.5 shrink-0 ${changed.has(rel) ? "text-emerald-600" : "text-gray-400"}`} />
              <span className="truncate">{baseName(rel)}</span>
              {dirty && <span className="w-1.5 h-1.5 rounded-full bg-indigo-500 shrink-0" />}
              <button onClick={(e) => { e.stopPropagation(); onCloseTab?.(rel); }}
                className="p-0.5 rounded text-gray-400 opacity-0 group-hover:opacity-100 hover:text-red-600 shrink-0"><X className="w-3 h-3" /></button>
            </div>
          );
        })}
        <div className="flex-1" />
        <button onClick={onClose} title="Hide editor panel" className="px-2 text-gray-400 hover:text-gray-700 shrink-0">
          <PanelRightClose className="w-4 h-4" />
        </button>
      </div>
      )}

      {/* Action bar: path · Edit/Diff toggle · Save / Revert */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-gray-100 shrink-0 text-xs min-w-0">
        <span className="font-mono text-gray-500 truncate min-w-0" title={activeFile}>{activeFile}</span>
        {buf?.dirty && <span className="text-indigo-500 shrink-0">●&nbsp;unsaved</span>}
        <div className="flex-1" />
        {(hasDiff || isPreviewable) && (
          <div className="flex items-center rounded-md border border-gray-200 overflow-hidden shrink-0">
            <button onClick={() => onSetMode?.("edit")}
              className={`px-2 py-0.5 ${effectiveMode === "edit" ? "bg-indigo-600 text-white" : "text-gray-600 hover:bg-gray-100"}`}>Edit</button>
            {hasDiff && (
              <button onClick={() => onSetMode?.("diff")}
                className={`px-2 py-0.5 flex items-center gap-1 border-l border-gray-200 ${effectiveMode === "diff" ? "bg-indigo-600 text-white" : "text-gray-600 hover:bg-gray-100"}`}>
                <FileDiff className="w-3 h-3" /> Diff</button>
            )}
            {isPreviewable && (
              <button onClick={() => onSetMode?.("preview")}
                className={`px-2 py-0.5 flex items-center gap-1 border-l border-gray-200 ${effectiveMode === "preview" ? "bg-indigo-600 text-white" : "text-gray-600 hover:bg-gray-100"}`}>
                <Eye className="w-3 h-3" /> Preview</button>
            )}
          </div>
        )}
        {isPendingHere ? (
          // The agent's edit to THIS file is awaiting approval — same decision as
          // the chat's permission bar, surfaced here since the diff covers it.
          <>
            <button onClick={() => onAnswer?.("no")}
              className="flex items-center gap-1 px-2 py-0.5 rounded border border-gray-300 text-gray-700 bg-white hover:bg-gray-50 shrink-0">
              <Ban className="w-3.5 h-3.5" /> Reject</button>
            <button onClick={() => onAnswer?.("always")}
              className="px-2 py-0.5 rounded border border-amber-400 text-amber-800 bg-white hover:bg-amber-100 shrink-0">Always</button>
            <button onClick={() => onAnswer?.("yes")}
              className="flex items-center gap-1 px-2 py-0.5 rounded bg-emerald-600 text-white hover:bg-emerald-700 shrink-0">
              <Check className="w-3.5 h-3.5" /> Accept</button>
          </>
        ) : (
          <>
            <button onClick={() => reload(activeFile)} title="Reload from disk"
              className="flex items-center gap-1 px-2 py-0.5 rounded text-gray-500 hover:bg-gray-100 shrink-0"><RotateCcw className="w-3.5 h-3.5" /></button>
            <button onClick={() => save()} disabled={!buf?.dirty || saving}
              className="flex items-center gap-1 px-2 py-0.5 rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-30 shrink-0">
              {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />} Save
            </button>
          </>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 min-h-0 min-w-0 overflow-hidden">
        {!buf ? (
          <div className="h-full flex items-center justify-center text-gray-400 text-sm"><Loader2 className="w-4 h-4 animate-spin mr-2" /> Loading…</div>
        ) : buf?.error ? (
          <div className="h-full flex flex-col items-center justify-center text-red-500 text-sm gap-1 px-6 text-center">
            <AlertCircle className="w-5 h-5" /> {buf.error}
          </div>
        ) : effectiveMode === "preview" ? (
          // Live render of HTML/SVG in a sandboxed iframe (scripts allowed for
          // canvas/JS art; no same-origin → can't reach the app). Uses the live
          // buffer so unsaved edits preview too.
          <iframe title="preview" sandbox="allow-scripts allow-popups allow-modals"
            srcDoc={buf?.content ?? ""} className="w-full h-full border-0 bg-white" />
        ) : effectiveMode === "diff" ? (
          (before != null && after != null && after !== before) ? (
            // Full-file inline diff (VSCode/IntelliJ-style): the whole file with
            // the agent's change highlighted in context. original = pre-edit
            // snapshot, doc = proposed result (shown even before the edit is
            // applied, by reconstructing the result from the hunk).
            <CodeMirror
              value={after}
              theme={cmTheme}
              height="100%"
              style={{ height: "100%", width: "100%", fontSize: "12.5px" }}
              editable={false}
              extensions={[
                ...(langExt ? [langExt] : []),
                unifiedMergeView({ original: before, mergeControls: false, collapseUnchanged: { margin: 3, minSize: 4 } }),
                diffHighlightTheme,
              ]}
              basicSetup={{ lineNumbers: true, highlightActiveLine: false, foldGutter: false, autocompletion: false }}
            />
          ) : (
            // Fallback to the streamed hunk if we couldn't reconstruct the file.
            <div className="h-full overflow-auto"><DiffLines lines={entry?.lines || []} truncated={entry?.truncated || 0} /></div>
          )
        ) : (
          <CodeMirror
            value={buf?.content ?? ""}
            theme={cmTheme}
            height="100%"
            style={{ height: "100%", fontSize: "12.5px" }}
            extensions={langExt ? [langExt] : []}
            onChange={(val) => setContent(activeFile, val)}
            basicSetup={{ lineNumbers: true, highlightActiveLine: true, foldGutter: true, autocompletion: false }}
          />
        )}
      </div>
    </div>
  );
}
