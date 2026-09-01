// SPDX-License-Identifier: Apache-2.0
/* FileExplorer — lite-IDE project tree for the Code tab.
 *
 * Pure UI: it takes a flat list of "/"-relative file paths (already fetched by
 * Code.jsx via listFolder) and renders a collapsible tree. All filesystem
 * mutations are delegated to callbacks (onCreate/onRename/onDelete) so the
 * parent owns the desktop IPC + refresh. Paths in/out are always "/"-relative
 * to the workspace root; Code.jsx converts to OS-absolute paths.
 */
import { useMemo, useState } from "react";
import {
  ChevronRight, ChevronDown, FolderOpen, Folder, FileText,
  FilePlus, FolderPlus, Pencil, Trash2, Search, X, RefreshCw,
} from "lucide-react";

// Build a nested tree {name, rel, type, children:Map} from "/"-relative paths.
function buildTree(files) {
  const root = { name: "", rel: "", type: "dir", children: new Map() };
  for (const f of files) {
    const parts = String(f).split("/").filter(Boolean);
    let node = root, cur = "";
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      cur = cur ? cur + "/" + p : p;
      const isLast = i === parts.length - 1;
      if (!node.children.has(p)) {
        node.children.set(p, { name: p, rel: cur, type: isLast ? "file" : "dir", children: new Map() });
      }
      node = node.children.get(p);
    }
  }
  return root;
}

// Sorted children: directories first, then files, each alphabetical.
function sortedChildren(node) {
  return [...node.children.values()].sort((a, b) =>
    a.type !== b.type ? (a.type === "dir" ? -1 : 1) : a.name.localeCompare(b.name));
}

// Set of directory rels that contain at least one changed descendant file.
function changedDirs(changed) {
  const dirs = new Set();
  for (const rel of changed) {
    const parts = rel.split("/"); parts.pop();
    let cur = "";
    for (const p of parts) { cur = cur ? cur + "/" + p : p; dirs.add(cur); }
  }
  return dirs;
}

export default function FileExplorer({
  files = [], changed, activeFile, onOpen,
  onCreate, onRename, onDelete, onRefresh, refreshing = false,
}) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(() => new Set([""]));
  const [editing, setEditing] = useState(null); // {kind:'create-file'|'create-folder'|'rename', parentRel, rel, name}

  const changedSet = useMemo(() => changed || new Set(), [changed]);
  const q = query.trim().toLowerCase();
  const shownFiles = useMemo(
    () => (q ? files.filter((f) => String(f).toLowerCase().includes(q)) : files),
    [files, q]);
  const tree = useMemo(() => buildTree(shownFiles), [shownFiles]);
  const cDirs = useMemo(() => changedDirs(changedSet), [changedSet]);

  const isExpanded = (rel) => q ? true : expanded.has(rel); // search auto-expands
  const toggle = (rel) => setExpanded((s) => {
    const n = new Set(s); n.has(rel) ? n.delete(rel) : n.add(rel); return n;
  });

  const startCreate = (kind, parentRel) => {
    if (parentRel) setExpanded((s) => new Set(s).add(parentRel));
    setEditing({ kind, parentRel, name: "" });
  };
  const commitEdit = () => {
    if (!editing) return;
    const name = (editing.name || "").trim();
    if (!name) { setEditing(null); return; }
    if (editing.kind === "rename") {
      const parent = editing.rel.includes("/") ? editing.rel.slice(0, editing.rel.lastIndexOf("/")) : "";
      const next = parent ? `${parent}/${name}` : name;
      if (next !== editing.rel) onRename?.(editing.rel, next);
    } else {
      const rel = editing.parentRel ? `${editing.parentRel}/${name}` : name;
      onCreate?.(rel, editing.kind === "create-folder");
    }
    setEditing(null);
  };

  const EditRow = ({ depth, isFolder }) => (
    <div className="flex items-center gap-1 px-1 py-0.5" style={{ paddingLeft: depth * 12 + 8 }}>
      {isFolder
        ? <Folder className="w-3.5 h-3.5 text-gray-400 shrink-0" />
        : <FileText className="w-3.5 h-3.5 text-gray-400 shrink-0" />}
      <input autoFocus value={editing.name}
        onChange={(e) => setEditing((ed) => ({ ...ed, name: e.target.value }))}
        onKeyDown={(e) => { if (e.key === "Enter") commitEdit(); else if (e.key === "Escape") setEditing(null); }}
        onBlur={commitEdit}
        placeholder={editing.kind === "create-folder" ? "folder name" : "file name"}
        className="flex-1 min-w-0 text-sm bg-white border border-indigo-300 rounded px-1 py-0.5 outline-none" />
    </div>
  );

  const renderNode = (node, depth) => {
    const rows = [];
    // Inline create input sits at the top of its parent folder's children.
    if (editing && editing.kind !== "rename" && editing.parentRel === node.rel) {
      rows.push(<EditRow key="__new" depth={depth} isFolder={editing.kind === "create-folder"} />);
    }
    for (const child of sortedChildren(node)) {
      const pad = { paddingLeft: depth * 12 + 8 };
      if (editing && editing.kind === "rename" && editing.rel === child.rel) {
        rows.push(<EditRow key={child.rel} depth={depth} isFolder={child.type === "dir"} />);
        if (child.type === "dir" && isExpanded(child.rel)) rows.push(...renderNode(child, depth + 1));
        continue;
      }
      if (child.type === "dir") {
        const open = isExpanded(child.rel);
        rows.push(
          <div key={child.rel} className="group flex items-center gap-1 rounded hover:bg-gray-100 cursor-pointer"
            style={pad} onClick={() => toggle(child.rel)}>
            {open ? <ChevronDown className="w-3.5 h-3.5 text-gray-400 shrink-0" /> : <ChevronRight className="w-3.5 h-3.5 text-gray-400 shrink-0" />}
            {open ? <FolderOpen className="w-3.5 h-3.5 text-indigo-500 shrink-0" /> : <Folder className="w-3.5 h-3.5 text-indigo-500 shrink-0" />}
            <span className="text-sm text-gray-800 truncate flex-1">{child.name}</span>
            {!open && cDirs.has(child.rel) && <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0 mr-1" />}
            <span className="hidden group-hover:flex items-center gap-0.5 shrink-0">
              <button title="New file" onClick={(e) => { e.stopPropagation(); startCreate("create-file", child.rel); }}
                className="p-0.5 text-gray-400 hover:text-indigo-600"><FilePlus className="w-3.5 h-3.5" /></button>
              <button title="New folder" onClick={(e) => { e.stopPropagation(); startCreate("create-folder", child.rel); }}
                className="p-0.5 text-gray-400 hover:text-indigo-600"><FolderPlus className="w-3.5 h-3.5" /></button>
              <button title="Rename" onClick={(e) => { e.stopPropagation(); setEditing({ kind: "rename", rel: child.rel, name: child.name }); }}
                className="p-0.5 text-gray-400 hover:text-gray-700"><Pencil className="w-3 h-3" /></button>
              <button title="Delete" onClick={(e) => { e.stopPropagation(); onDelete?.(child.rel, "dir"); }}
                className="p-0.5 text-gray-400 hover:text-red-600"><Trash2 className="w-3.5 h-3.5" /></button>
            </span>
          </div>
        );
        if (open) rows.push(...renderNode(child, depth + 1));
      } else {
        const isChanged = changedSet.has(child.rel);
        const isActive = activeFile === child.rel;
        rows.push(
          <div key={child.rel}
            className={`group flex items-center gap-1 rounded cursor-pointer ${isActive ? "bg-indigo-100" : "hover:bg-gray-100"}`}
            style={pad} onClick={() => onOpen?.(child.rel)}>
            <span className="w-3.5 shrink-0" />
            <FileText className={`w-3.5 h-3.5 shrink-0 ${isChanged ? "text-emerald-600" : "text-gray-400"}`} />
            <span className={`text-sm truncate flex-1 ${isActive ? "text-indigo-800 font-medium" : "text-gray-700"}`}>{child.name}</span>
            {isChanged && <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0 mr-1" />}
            <span className="hidden group-hover:flex items-center gap-0.5 shrink-0">
              <button title="Rename" onClick={(e) => { e.stopPropagation(); setEditing({ kind: "rename", rel: child.rel, name: child.name }); }}
                className="p-0.5 text-gray-400 hover:text-gray-700"><Pencil className="w-3 h-3" /></button>
              <button title="Delete" onClick={(e) => { e.stopPropagation(); onDelete?.(child.rel, "file"); }}
                className="p-0.5 text-gray-400 hover:text-red-600"><Trash2 className="w-3.5 h-3.5" /></button>
            </span>
          </div>
        );
      }
    }
    return rows;
  };

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Toolbar: search + new file/folder at root */}
      <div className="px-2 pt-2 pb-1 space-y-1.5">
        <div className="flex items-center gap-1 bg-white border border-gray-200 rounded-md px-1.5">
          <Search className="w-3.5 h-3.5 text-gray-400 shrink-0" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search files"
            className="flex-1 min-w-0 text-xs py-1 outline-none bg-transparent" />
          {query && <button onClick={() => setQuery("")} className="p-0.5 text-gray-400 hover:text-gray-700"><X className="w-3 h-3" /></button>}
        </div>
        <div className="flex items-center gap-1">
          <button title="New file in root" onClick={() => startCreate("create-file", "")}
            className="flex-1 flex items-center justify-center gap-1 text-[11px] text-gray-600 border border-gray-200 rounded px-1.5 py-1 hover:bg-gray-100">
            <FilePlus className="w-3.5 h-3.5" /> File
          </button>
          <button title="New folder in root" onClick={() => startCreate("create-folder", "")}
            className="flex-1 flex items-center justify-center gap-1 text-[11px] text-gray-600 border border-gray-200 rounded px-1.5 py-1 hover:bg-gray-100">
            <FolderPlus className="w-3.5 h-3.5" /> Folder
          </button>
          <button title="Refresh from local disk" onClick={() => onRefresh?.()} disabled={refreshing || !onRefresh}
            className="flex items-center justify-center gap-1 text-[11px] text-gray-600 border border-gray-200 rounded px-1.5 py-1 hover:bg-gray-100 disabled:opacity-40">
            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>
      {/* Tree */}
      <div className="flex-1 overflow-y-auto px-1 pb-2">
        {files.length === 0 ? (
          <p className="text-xs text-gray-400 px-2 mt-2">No files indexed yet.</p>
        ) : shownFiles.length === 0 ? (
          <p className="text-xs text-gray-400 px-2 mt-2">No matches for “{query}”.</p>
        ) : (
          renderNode(tree, 0)
        )}
      </div>
    </div>
  );
}
