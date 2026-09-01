// SPDX-License-Identifier: Apache-2.0
// ManifestView — displayed at AWAITING_CODE_APPROVAL (legacy: AWAITING_DESIGN_APPROVAL) showing the change manifest.
import { useState } from "react";
import { Copy, Check, ChevronDown, ChevronRight } from "lucide-react";
import DiffApprovalPanel from "./DiffApprovalPanel.jsx";

function TypeBadge({ type }) {
  const styles = {
    MODIFY: "bg-blue-100 text-blue-700",
    CREATE: "bg-green-100 text-green-700",
    DELETE: "bg-red-100 text-red-700",
  };
  const t = (type || "MODIFY").toUpperCase();
  return <span className={`px-2 py-0.5 rounded text-xs font-medium ${styles[t] || "bg-gray-100 text-gray-600"}`}>{t}</span>;
}

function FilePath({ path }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(path);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  return (
    <div className="flex items-center gap-1 group">
      <code className="text-xs text-gray-700 font-mono">{path}</code>
      <button onClick={copy} className="opacity-0 group-hover:opacity-100 transition-opacity text-gray-400 hover:text-gray-700">
        {copied ? <Check size={12} /> : <Copy size={12} />}
      </button>
    </div>
  );
}

function FileCard({ fc }) {
  const [open, setOpen] = useState(false);
  const path = fc.path || fc.file || "";
  const changeType = fc.change_type || fc.type || "MODIFY";
  const desc = fc.change_description || fc.description || "";
  const fn = fc.affected_function || fc.function_name || "";
  const newCode = fc.new_code || fc.code_spec || "";

  return (
    <div className="border border-gray-200 rounded-lg p-3 bg-white">
      <div className="flex items-start gap-2 flex-wrap">
        <TypeBadge type={changeType} />
        <FilePath path={path} />
      </div>
      {fn && <p className="text-xs text-gray-500 mt-1 font-mono">{fn}</p>}
      {desc && <p className="text-sm text-gray-600 mt-1">{desc}</p>}
      {newCode && (
        <div className="mt-2">
          <button
            className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800"
            onClick={() => setOpen(o => !o)}
          >
            {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            {open ? "Hide" : "Show"} code spec
          </button>
          {open && (
            <pre className="mt-1 text-xs bg-gray-900 text-gray-100 rounded p-2 overflow-x-auto max-h-48">{newCode}</pre>
          )}
        </div>
      )}
    </div>
  );
}

export default function ManifestView({ run }) {
  const ctx = run?.context || {};
  const design = ctx.design || {};
  const validationPass = ctx.manifest_validation_pass;
  // Shift-left: when a pre-gate VERIFIED_DIFF exists, the human approves the real
  // compiled+tested diff. The manifest below remains as a high-level summary.
  const verifiedDiff = (
    <DiffApprovalPanel run={run} />
  );

  const fileChanges = design.file_changes || design.files_to_change || [];
  const newFiles = design.new_files_needed || [];
  const deleteFiles = design.files_to_delete || [];
  const totalFiles = fileChanges.length + newFiles.length + deleteFiles.length;

  if (totalFiles === 0) return verifiedDiff;

  return (
    <>
    {verifiedDiff}
    <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-lg">📝</span>
        <h3 className="font-semibold text-gray-800">Change Manifest — Review Before Approving</h3>
        {validationPass === true && (
          <span className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full">✓ Validated</span>
        )}
        {validationPass === false && (
          <span className="text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full">⚠ Issues found</span>
        )}
      </div>
      <p className="text-xs text-gray-500 mb-3">
        {fileChanges.length} file(s) to modify
        {newFiles.length > 0 && `, ${newFiles.length} to create`}
        {deleteFiles.length > 0 && `, ${deleteFiles.length} to delete`}
      </p>

      <div className="space-y-2">
        {fileChanges.map((fc, i) => (
          <FileCard key={i} fc={typeof fc === 'string' ? { path: fc } : fc} />
        ))}
        {newFiles.map((f, i) => (
          <FileCard key={`new-${i}`} fc={{ path: typeof f === 'string' ? f : f.path, change_type: "CREATE", description: typeof f === 'string' ? "" : f.description }} />
        ))}
      </div>
    </div>
    </>
  );
}
