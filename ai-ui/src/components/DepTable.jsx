// SPDX-License-Identifier: MIT
// DepTable — dependency declaration shown in TriggerModal when
// VITE_ENABLE_MULTI_REPO_SDLC=true.
//
// Props:
//   deps      — array of { repo, ref, kind, source }
//   onChange  — (newDeps) => void — called whenever the table changes

import { useState, useEffect, useRef } from "react";
import { PlusCircle, Trash2, Loader2 } from "lucide-react";
import { API_BASE as API, apiFetch } from "../config";

const KIND_OPTIONS = ["compile-only", "editable"];

const SOURCE_BADGE = {
  manifest:     { label: "manifest",   cls: "bg-blue-100 text-blue-700" },
  user:         { label: "user",       cls: "bg-green-100 text-green-700" },
  "build-file": { label: "build-file", cls: "bg-gray-100 text-gray-600" },
};

function sourceBadge(source) {
  const s = SOURCE_BADGE[source] || { label: source || "user", cls: "bg-gray-100 text-gray-600" };
  return (
    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${s.cls}`}>
      {s.label}
    </span>
  );
}

function isValidRepo(val) {
  return /^[a-zA-Z0-9_.-]+\/[a-zA-Z0-9_./-]+$/.test(val.trim());
}

export default function DepTable({ deps, onChange, primaryRepo, primaryBranch }) {
  const [loadingManifest, setLoadingManifest] = useState(false);
  const lastFetchedRepo = useRef(null);

  useEffect(() => {
    if (!primaryRepo || !primaryRepo.trim()) return;
    const repo = primaryRepo.trim();
    if (lastFetchedRepo.current === repo) return;
    lastFetchedRepo.current = repo;

    setLoadingManifest(true);
    const ref = (primaryBranch || "").trim() || undefined;
    const url = ref
      ? `${API}/sdlc/repo/${encodeURIComponent(repo)}/dependencies?ref=${encodeURIComponent(ref)}`
      : `${API}/sdlc/repo/${encodeURIComponent(repo)}/dependencies`;

    apiFetch(url)
      .then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(d => {
        const fetched = (d.dependencies || []).map(dep => ({
          repo: dep.repo || "",
          ref: dep.ref || "",
          kind: dep.kind || "compile-only",
          source: dep.source || "manifest",
        }));
        const userRows = deps.filter(row => row.source === "user");
        const merged = [
          ...fetched.filter(mRow => !userRows.some(u => u.repo === mRow.repo)),
          ...userRows,
        ];
        onChange(merged);
      })
      .catch(() => {})
      .finally(() => setLoadingManifest(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [primaryRepo]);

  function addRow() {
    onChange([
      ...deps,
      { repo: "", ref: "", kind: "compile-only", source: "user" },
    ]);
  }

  function removeRow(idx) {
    onChange(deps.filter((_, i) => i !== idx));
  }

  function updateRow(idx, field, value) {
    onChange(
      deps.map((row, i) =>
        i === idx ? { ...row, [field]: value, source: "user" } : row
      )
    );
  }

  return (
    <div className="mt-3">
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs font-medium text-gray-600 uppercase tracking-wide">
          Dependencies
        </p>
        {loadingManifest && (
          <span className="flex items-center gap-1 text-[10px] text-gray-400">
            <Loader2 size={10} className="animate-spin" /> Loading manifest...
          </span>
        )}
      </div>

      {deps.length === 0 ? (
        <p className="text-xs text-gray-400 italic mb-2">
          No dependencies declared. Add entries below or they will be inferred from the build file.
        </p>
      ) : (
        <div className="flex flex-col gap-2 mb-2">
          {deps.map((row, idx) => {
            const repoErr = row.repo && !isValidRepo(row.repo);
            return (
              <div
                key={idx}
                className="border border-gray-200 rounded-lg p-3 bg-white flex flex-col gap-2"
              >
                {/* Header: index + source badge + remove */}
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide">
                    Dependency {idx + 1}
                  </span>
                  <div className="flex items-center gap-2">
                    {sourceBadge(row.source)}
                    <button
                      type="button"
                      onClick={() => removeRow(idx)}
                      className="text-gray-300 hover:text-red-500 transition-colors"
                      title="Remove"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>

                {/* Repo path */}
                <div>
                  <label className="block text-xs text-gray-500 mb-0.5">Repo path</label>
                  <input
                    className={`w-full border rounded px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1
                      ${repoErr
                        ? "border-red-300 focus:ring-red-300"
                        : "border-gray-200 focus:ring-indigo-300"
                      }`}
                    placeholder="group/project"
                    value={row.repo}
                    onChange={e => updateRow(idx, "repo", e.target.value)}
                  />
                  {repoErr && (
                    <p className="text-[10px] text-red-500 mt-0.5">Use group/project format</p>
                  )}
                </div>

                {/* Ref */}
                <div>
                  <label className="block text-xs text-gray-500 mb-0.5">Ref / branch</label>
                  <input
                    className="w-full border border-gray-200 rounded px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-indigo-300"
                    placeholder="main"
                    value={row.ref}
                    onChange={e => updateRow(idx, "ref", e.target.value)}
                  />
                </div>

                {/* Kind */}
                <div>
                  <label className="block text-xs text-gray-500 mb-0.5">Kind</label>
                  <select
                    className="w-full border border-gray-200 rounded px-2.5 py-1.5 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-indigo-300"
                    value={row.kind}
                    onChange={e => updateRow(idx, "kind", e.target.value)}
                  >
                    {KIND_OPTIONS.map(k => (
                      <option key={k} value={k}>{k}</option>
                    ))}
                  </select>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <button
        type="button"
        onClick={addRow}
        className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800 font-medium"
      >
        <PlusCircle size={13} /> Add dependency
      </button>
    </div>
  );
}
