// SPDX-License-Identifier: MIT
// ManifestValidationPanel — shown on the MANIFEST_VALIDATION sub-check (rendered
// inside the PLAN drawer, and as a compact banner when a run SUSPENDED at PLAN
// with a manifest-validation-failure reason).
import { CheckCircle2, XCircle, Circle, ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

function CollapsibleList({ title, items, variant }) {
  const [open, setOpen] = useState(false);
  if (!items || items.length === 0) return null;
  const cls = variant === 'error' ? 'text-red-600' : 'text-amber-600';
  return (
    <div className="mt-1">
      <button className={`flex items-center gap-1 text-xs ${cls} hover:underline`} onClick={() => setOpen(o => !o)}>
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {title} ({items.length})
      </button>
      {open && (
        <ul className="pl-4 mt-1 space-y-0.5">
          {items.map((it, i) => <li key={i} className="text-xs text-gray-600">{it}</li>)}
        </ul>
      )}
    </div>
  );
}

export default function ManifestValidationPanel({ artifact }) {
  if (!artifact) return null;
  const {
    passed, issues, struct_pass, openai_pass, struct_failures, openai_issues,
    hallucinated_paths, missing_components, oos_violations, skipped_reason,
  } = artifact;

  // openai_pass === null (or absent) means the OpenAI cross-check was SKIPPED
  // (e.g. complexity=="simple", or compliance-blocked) — not pass, not fail.
  const openaiSkipped = openai_pass === null || openai_pass === undefined;
  const overallPass = typeof passed === "boolean" ? passed : (struct_pass !== false && openai_pass !== false);

  return (
    <div className="bg-white border border-violet-200 rounded-lg p-3 text-sm">
      <div className="flex items-center gap-2 mb-2">
        {overallPass
          ? <CheckCircle2 size={16} className="text-green-500" />
          : <XCircle size={16} className="text-red-500" />}
        <span className="font-medium text-gray-800">Manifest Validation</span>
        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${overallPass ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
          {overallPass ? 'PASS' : 'REJECT'}
        </span>
      </div>

      <CollapsibleList title="Issues" items={issues} variant="error" />

      <div className="space-y-2">
        <div>
          <p className="text-xs font-semibold text-gray-500 mb-1">Structural Checks</p>
          <div className="flex items-center gap-2">
            {struct_pass === false
              ? <XCircle size={14} className="text-red-500" />
              : <CheckCircle2 size={14} className="text-green-500" />}
            <span className="text-xs text-gray-600">
              {struct_pass === false ? `${(struct_failures || []).length} failure(s)` : 'All paths verified'}
            </span>
          </div>
          {(struct_failures || []).map((f, i) => (
            <p key={i} className="text-xs text-red-600 pl-5 mt-0.5">{f}</p>
          ))}
        </div>

        <div>
          <p className="text-xs font-semibold text-gray-500 mb-1">OpenAI Cross-Validation</p>
          <div className="flex items-center gap-2">
            {openaiSkipped
              ? <Circle size={14} className="text-gray-300" />
              : openai_pass === false
              ? <XCircle size={14} className="text-red-500" />
              : <CheckCircle2 size={14} className="text-green-500" />}
            <span className="text-xs text-gray-600">
              {openaiSkipped
                ? `Skipped${skipped_reason ? ` — ${skipped_reason}` : ''}`
                : openai_pass === false ? 'Issues found' : 'No issues found'}
            </span>
            {openaiSkipped && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-500 font-medium">SKIPPED</span>
            )}
          </div>
          <CollapsibleList title="Hallucinated paths" items={hallucinated_paths} variant="error" />
          <CollapsibleList title="Missing components" items={missing_components} variant="error" />
          <CollapsibleList title="Out-of-scope violations" items={oos_violations} variant="error" />
          <CollapsibleList title="Other issues" items={openai_issues} variant="error" />
        </div>
      </div>
    </div>
  );
}
