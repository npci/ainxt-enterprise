// SPDX-License-Identifier: Apache-2.0
// MultiRepoApprovalView — renders per-repo plan sections inside ApprovalPanel
// when the HITL payload contains a `repos` array (multi-repo runs).
//
// Rendered above the Approve / Reject buttons in ApprovalPanel.
// Falls back to nothing (returns null) if repos is absent or empty.
//
// Expected shape of each element in repos[]:
//   {
//     repo:                 string            — e.g. "ainxt/payments-sdk"
//     ref:                  string            — branch or tag
//     kind:                 "primary" | "editable" | "compile-only"
//     per_repo_plan:        string | null     — markdown text, shown for primary + editable
//     files_likely_to_change: string[]        — shown for primary + editable
//   }

import { useState } from "react";
import { ChevronDown, ChevronRight, GitBranch, Code2 } from "lucide-react";

// Minimal markdown-to-plain rendering: preserves structure without a dep.
// The existing codebase has no markdown renderer, so we render in a <pre> with
// light wrapping so the text stays readable.
function PlanText({ text }) {
  if (!text) return <p className="text-xs text-gray-400 italic">No plan detail available.</p>;
  return (
    <pre className="whitespace-pre-wrap font-sans text-xs text-gray-700 leading-relaxed max-h-64 overflow-y-auto bg-white border border-gray-100 rounded p-2">
      {text}
    </pre>
  );
}

function RepoCard({ entry }) {
  const [open, setOpen] = useState(true);
  const kindColor =
    entry.kind === "primary"  ? "bg-indigo-100 text-indigo-700" :
    entry.kind === "editable" ? "bg-green-100 text-green-700"   :
                                "bg-gray-100 text-gray-500";
  const files = entry.files_likely_to_change || [];

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 bg-gray-50 hover:bg-gray-100 text-left transition-colors"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <GitBranch size={12} className="text-gray-400 flex-shrink-0" />
        <span className="text-xs font-medium text-gray-800 flex-1 truncate">
          {entry.repo}
          {entry.ref ? <span className="ml-1 font-normal text-gray-400">@{entry.ref}</span> : null}
        </span>
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium flex-shrink-0 ${kindColor}`}>
          {entry.kind}
        </span>
      </button>

      {open && (
        <div className="px-3 py-2 space-y-2">
          <div>
            <p className="text-[10px] font-medium text-gray-400 uppercase tracking-wide mb-1">
              Design / plan
            </p>
            <PlanText text={entry.per_repo_plan} />
          </div>

          {files.length > 0 && (
            <div>
              <p className="text-[10px] font-medium text-gray-400 uppercase tracking-wide mb-1">
                Files likely to change
              </p>
              <ul className="space-y-0.5 max-h-32 overflow-y-auto">
                {files.map((f, i) => (
                  <li key={i} className="flex items-center gap-1 font-mono text-[11px] text-indigo-700 truncate">
                    <Code2 size={9} className="flex-shrink-0 text-gray-300" />
                    {f}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function MultiRepoApprovalView({ repos }) {
  if (!repos || repos.length === 0) return null;

  const editableRepos = repos.filter(r => r.kind === "primary" || r.kind === "editable");
  const compileOnly  = repos.filter(r => r.kind === "compile-only");

  return (
    <div className="mb-3 space-y-2">
      <p className="text-xs font-semibold text-yellow-800">
        Multi-repo run — {editableRepos.length} editable repo{editableRepos.length !== 1 ? "s" : ""}
        {compileOnly.length > 0 ? `, ${compileOnly.length} compile-only` : ""}
      </p>

      {editableRepos.map((entry, i) => (
        <RepoCard key={`${entry.repo}-${i}`} entry={entry} />
      ))}

      {compileOnly.length > 0 && (
        <div className="border border-gray-200 rounded-lg px-3 py-2 bg-gray-50">
          <p className="text-[10px] font-medium text-gray-500 uppercase tracking-wide mb-1">
            Compile-only deps ({compileOnly.length})
          </p>
          <p className="text-xs text-gray-600 leading-relaxed">
            {compileOnly.map(r => `${r.repo}${r.ref ? `@${r.ref}` : ""}`).join(", ")}
          </p>
          <p className="text-[10px] text-gray-400 mt-1 italic">
            These repos are built inside the sandbox for classpath resolution only — no code changes or MRs.
          </p>
        </div>
      )}
    </div>
  );
}
