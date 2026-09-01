// SPDX-License-Identifier: Apache-2.0
// PlanningArtifactView — renders the SDLC PLAN artifact (three-phase CLI engine:
// PLAN / IMPLEMENT / REVIEW). The plan dict carries the union of the old
// analyst+designer keys (files_to_change, solution_approach, implementation_plan,
// testing_strategy, etc.) plus open_questions/affected_components.
// Data source: GET /sdlc/runs/{runId}/stages/PLAN/artifact
//   → fallback: run.context.{analysis,design} (both mirror the PLAN artifact)
import { useState, useEffect } from "react";
import {
  CheckCircle2,
  XCircle,
  ChevronDown,
  ChevronRight,
  FileText,
  FilePlus,
  HelpCircle,
  Lightbulb,
  AlertTriangle,
  Copy,
  Check,
} from "lucide-react";
import { API_BASE as API, apiFetch } from "../../config";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Coerce str | dict | list | null to a human-readable string. */
function _s(val) {
  if (val == null) return "";
  if (typeof val === "string") return val;
  if (Array.isArray(val)) return val.map(_s).filter(Boolean).join("; ");
  if (typeof val === "object") {
    return Object.entries(val)
      .map(([k, v]) => `${k}: ${_s(v)}`)
      .join(" | ");
  }
  return String(val);
}

/** True if a value counts as "populated" (non-null, non-empty). */
function isPopulated(val) {
  if (val == null) return false;
  if (typeof val === "string") return val.trim().length > 0;
  if (Array.isArray(val)) return val.length > 0;
  if (typeof val === "object") return Object.keys(val).length > 0;
  return Boolean(val);
}

/** Render a value that may be a string, array of strings, or dict. */
function AnyValue({ val, className = "text-sm text-gray-700" }) {
  if (val == null || val === "") return null;
  if (typeof val === "string") return <p className={className}>{val}</p>;
  if (Array.isArray(val)) {
    return (
      <ul className="list-disc pl-4 space-y-0.5">
        {val.map((item, i) => (
          <li key={i} className={className}>
            {_s(item)}
          </li>
        ))}
      </ul>
    );
  }
  if (typeof val === "object") {
    return (
      <div className="space-y-0.5">
        {Object.entries(val).map(([k, v]) => (
          <p key={k} className={className}>
            <span className="font-medium text-gray-500">{k}:</span>{" "}
            {_s(v)}
          </p>
        ))}
      </div>
    );
  }
  return <p className={className}>{String(val)}</p>;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Section({ icon, title, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-4 py-3 bg-gray-50 hover:bg-gray-100 text-left"
        onClick={() => setOpen((o) => !o)}
      >
        {icon}
        <span className="font-semibold text-gray-800 text-sm flex-1">{title}</span>
        {open ? (
          <ChevronDown size={14} className="text-gray-400" />
        ) : (
          <ChevronRight size={14} className="text-gray-400" />
        )}
      </button>
      {open && <div className="px-4 py-3 space-y-3">{children}</div>}
    </div>
  );
}

function CollapsibleSection({ icon, title, children }) {
  return (
    <Section icon={icon} title={title} defaultOpen={false}>
      {children}
    </Section>
  );
}

/** Coverage chip: green if populated, muted gray if missing. */
function CoverageChip({ label, populated }) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
        populated
          ? "bg-green-100 text-green-700"
          : "bg-gray-100 text-gray-400"
      }`}
    >
      {populated ? <CheckCircle2 size={10} /> : <XCircle size={10} />}
      {label}
    </span>
  );
}

function CopyPath({ path }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(path).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  return (
    <div className="flex items-center gap-1 group">
      <code className="text-xs text-gray-700 font-mono break-all">{path}</code>
      <button
        onClick={copy}
        className="opacity-0 group-hover:opacity-100 transition-opacity text-gray-400 hover:text-gray-700"
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
      </button>
    </div>
  );
}

function FileEntry({ entry, badge }) {
  const path =
    typeof entry === "string"
      ? entry
      : entry?.path || entry?.file || "";
  const desc =
    typeof entry === "string"
      ? ""
      : entry?.change_desc || entry?.description || entry?.change_description || "";
  // grounding evidence may live under any of these keys
  const evidence =
    typeof entry === "object"
      ? entry?.evidence || entry?.grounding || entry?.evidence_path || null
      : null;

  return (
    <div className="border border-gray-200 rounded-lg p-3 bg-white">
      <div className="flex items-start gap-2 flex-wrap">
        {badge && (
          <span className={`px-2 py-0.5 rounded text-xs font-medium ${badge.cls}`}>
            {badge.label}
          </span>
        )}
        <CopyPath path={path} />
      </div>
      {desc && <p className="text-sm text-gray-600 mt-1">{desc}</p>}
      {evidence && (
        <p className="text-xs text-gray-400 mt-1 font-mono">
          ↳ {_s(evidence)}
        </p>
      )}
    </div>
  );
}

function OpenQuestion({ q, idx }) {
  const text =
    typeof q === "string" ? q : q?.question || q?.text || _s(q);
  const options = Array.isArray(q?.options) ? q.options : [];
  const recommended = q?.recommended;
  const rationale = q?.rationale || "";

  return (
    <div className="border border-amber-200 rounded-lg p-3 bg-amber-50">
      <p className="text-sm font-medium text-amber-900">
        Q{idx + 1}. {text}
      </p>
      {options.length > 0 && (
        <ul className="mt-2 space-y-1">
          {options.map((opt, i) => {
            const optStr = _s(opt);
            const isRec =
              recommended != null && (optStr === _s(recommended) || i === recommended);
            return (
              <li
                key={i}
                className={`text-xs flex items-start gap-1.5 ${
                  isRec ? "font-semibold text-amber-800" : "text-gray-600"
                }`}
              >
                {isRec ? (
                  <CheckCircle2 size={12} className="mt-0.5 text-amber-600 flex-shrink-0" />
                ) : (
                  <span className="w-3 flex-shrink-0" />
                )}
                {optStr}
                {isRec && (
                  <span className="ml-1 text-amber-600 font-normal">(recommended)</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {rationale && (
        <p className="text-xs text-gray-500 mt-2 italic">{rationale}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Coverage strip
// ---------------------------------------------------------------------------

const ANALYST_KEYS = [
  { key: "files_to_change", label: "files_to_change" },
  { key: "sub_tasks", label: "sub_tasks" },
  { key: "implementation_spec", label: "implementation_spec" },
];

const DESIGNER_KEYS = [
  { key: "solution_approach", label: "solution_approach" },
  { key: "implementation_plan", label: "implementation_plan" },
  { key: "code_structure", label: "code_structure" },
  { key: "testing_strategy", label: "testing_strategy" },
  { key: "rollback_strategy", label: "rollback_strategy" },
];

function CoverageStrip({ artifact }) {
  const allKeys = [...ANALYST_KEYS, ...DESIGNER_KEYS];
  const populated = allKeys.filter((k) => isPopulated(artifact?.[k.key])).length;
  const total = allKeys.length;

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-3">
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Coverage
        </p>
        <span
          className={`text-xs font-medium px-2 py-0.5 rounded-full ${
            populated === total
              ? "bg-green-100 text-green-700"
              : populated >= total * 0.6
              ? "bg-amber-100 text-amber-700"
              : "bg-red-100 text-red-700"
          }`}
        >
          {populated}/{total} keys
        </span>
      </div>

      <div className="mb-1">
        <p className="text-xs text-gray-400 mb-1">Analyst</p>
        <div className="flex flex-wrap gap-1">
          {ANALYST_KEYS.map((k) => (
            <CoverageChip
              key={k.key}
              label={k.label}
              populated={isPopulated(artifact?.[k.key])}
            />
          ))}
        </div>
      </div>

      <div>
        <p className="text-xs text-gray-400 mb-1">Designer</p>
        <div className="flex flex-wrap gap-1">
          {DESIGNER_KEYS.map((k) => (
            <CoverageChip
              key={k.key}
              label={k.label}
              populated={isPopulated(artifact?.[k.key])}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Loading / Empty
// ---------------------------------------------------------------------------

function LoadingSkeleton() {
  return (
    <div className="space-y-3 animate-pulse">
      <div className="h-24 bg-gray-100 rounded-lg" />
      <div className="h-32 bg-gray-100 rounded-lg" />
      <div className="h-20 bg-gray-100 rounded-lg" />
    </div>
  );
}

function EmptyState({ message }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-gray-400">
      <FileText size={32} className="mb-2 opacity-40" />
      <p className="text-sm">{message || "No planning artifact available."}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Fetch logic
// ---------------------------------------------------------------------------

async function fetchArtifact(runId) {
  // PLAN is the single planning stage (three-phase CLI engine: PLAN / IMPLEMENT /
  // REVIEW) — ANALYZING/DESIGNING no longer exist, so there is no split-stage
  // fallback to compose.
  try {
    const r = await apiFetch(`${API}/sdlc/runs/${runId}/stages/PLAN/artifact`);
    if (r.ok) {
      const data = await r.json();
      return data?.payload ?? data;
    }
  } catch (_) {}

  return null;
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

/**
 * PlanningArtifactView
 * @param {{ run?: object, runId?: string }} props
 */
export default function PlanningArtifactView({ run, runId }) {
  const resolvedId = runId ?? run?.id;

  const [artifact, setArtifact] = useState(null);
  const [loading, setLoading] = useState(Boolean(resolvedId));
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!resolvedId) {
      // Try run.context fallback immediately
      const ctx = run?.context ?? {};
      const combined = {
        ...(ctx.analysis ?? {}),
        ...(ctx.design ?? {}),
      };
      if (Object.keys(combined).length > 0) {
        setArtifact(combined);
      }
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

    fetchArtifact(resolvedId)
      .then((data) => {
        if (cancelled) return;
        if (data && Object.keys(data).length > 0) {
          setArtifact(data);
        } else {
          // Final fallback: run.context
          const ctx = run?.context ?? {};
          const combined = {
            ...(ctx.analysis ?? {}),
            ...(ctx.design ?? {}),
          };
          setArtifact(Object.keys(combined).length > 0 ? combined : null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err?.message ?? "Fetch failed");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [resolvedId]); // eslint-disable-line react-hooks/exhaustive-deps

  // -------------------------------------------------------------------------
  // Render states
  // -------------------------------------------------------------------------

  if (loading) return <LoadingSkeleton />;
  if (error) {
    return (
      <div className="flex items-center gap-2 text-sm text-red-600 p-3 border border-red-200 rounded-lg bg-red-50">
        <AlertTriangle size={14} />
        <span>Failed to load planning artifact: {error}</span>
      </div>
    );
  }
  if (!artifact) return <EmptyState message="Plan artifact not yet available for this run." />;

  // -------------------------------------------------------------------------
  // Normalise list fields (may be missing or null)
  // -------------------------------------------------------------------------

  const filesToChange = Array.isArray(artifact.files_to_change)
    ? artifact.files_to_change
    : [];
  const newFiles = Array.isArray(artifact.new_files_needed)
    ? artifact.new_files_needed
    : [];
  const subTasks = Array.isArray(artifact.sub_tasks) ? artifact.sub_tasks : [];
  const openQuestions = Array.isArray(artifact.open_questions)
    ? artifact.open_questions
    : [];
  const decisions = Array.isArray(artifact.decisions) ? artifact.decisions : [];
  const rejectedAlts = Array.isArray(artifact.rejected_alternatives)
    ? artifact.rejected_alternatives
    : [];
  const assumptions = Array.isArray(artifact.assumptions)
    ? artifact.assumptions
    : [];

  // Design-detail fields
  const designFields = [
    { key: "solution_approach", label: "Solution Approach" },
    { key: "implementation_plan", label: "Implementation Plan" },
    { key: "code_structure", label: "Code Structure" },
    { key: "testing_strategy", label: "Testing Strategy" },
    { key: "rollback_strategy", label: "Rollback Strategy" },
    { key: "implementation_spec", label: "Implementation Spec" },
  ].filter((f) => isPopulated(artifact[f.key]));

  const hasFiles = filesToChange.length > 0 || newFiles.length > 0;
  const hasReasoning =
    decisions.length > 0 || rejectedAlts.length > 0 || assumptions.length > 0;

  return (
    <div className="space-y-3">
      {/* Header */}
      <div className="flex items-center gap-2">
        <Lightbulb size={18} className="text-indigo-500" />
        <h3 className="font-semibold text-gray-800">Planning Artifact</h3>
        <span className="text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full">
          PLAN
        </span>
      </div>

      {/* 1. Coverage strip */}
      <CoverageStrip artifact={artifact} />

      {/* 2. Files to change + new files */}
      {hasFiles && (
        <Section
          icon={<FileText size={15} className="text-blue-500" />}
          title={`Files (${filesToChange.length} to modify, ${newFiles.length} to create)`}
        >
          {filesToChange.map((entry, i) => (
            <FileEntry
              key={i}
              entry={entry}
              badge={{ label: "MODIFY", cls: "bg-blue-100 text-blue-700" }}
            />
          ))}
          {newFiles.map((entry, i) => (
            <FileEntry
              key={`new-${i}`}
              entry={entry}
              badge={{ label: "CREATE", cls: "bg-green-100 text-green-700" }}
            />
          ))}
        </Section>
      )}

      {/* 3. Sub-tasks */}
      {subTasks.length > 0 && (
        <Section
          icon={<CheckCircle2 size={15} className="text-green-500" />}
          title={`Sub-tasks (${subTasks.length})`}
        >
          <ul className="list-disc pl-4 space-y-1">
            {subTasks.map((t, i) => (
              <li key={i} className="text-sm text-gray-700">
                {_s(t)}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {/* 4. Open questions */}
      {openQuestions.length > 0 && (
        <Section
          icon={<HelpCircle size={15} className="text-amber-500" />}
          title={`Open Questions (${openQuestions.length})`}
        >
          <div className="space-y-2">
            {openQuestions.map((q, i) => (
              <OpenQuestion key={i} q={q} idx={i} />
            ))}
          </div>
        </Section>
      )}

      {/* 5. Design detail (collapsible, closed by default) */}
      {designFields.length > 0 && (
        <CollapsibleSection
          icon={<Lightbulb size={15} className="text-indigo-500" />}
          title="Design Detail"
        >
          {designFields.map(({ key, label }) => (
            <div key={key}>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                {label}
              </p>
              <AnyValue val={artifact[key]} />
            </div>
          ))}
        </CollapsibleSection>
      )}

      {/* 6. Reasoning (collapsible, closed by default) */}
      {hasReasoning && (
        <CollapsibleSection
          icon={<FilePlus size={15} className="text-violet-500" />}
          title="Reasoning"
        >
          {decisions.length > 0 && (
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                Decisions
              </p>
              <div className="space-y-2">
                {decisions.map((d, i) => {
                  const text = typeof d === "string" ? d : d?.text || _s(d);
                  const rationale =
                    typeof d === "object" ? d?.rationale || "" : "";
                  return (
                    <div key={i} className="border-l-2 border-indigo-200 pl-3">
                      <p className="text-sm text-gray-800">{text}</p>
                      {rationale && (
                        <p className="text-xs text-gray-500 mt-0.5">{rationale}</p>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {rejectedAlts.length > 0 && (
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                Rejected Alternatives
              </p>
              <div className="space-y-2">
                {rejectedAlts.map((a, i) => {
                  const alt = typeof a === "string" ? a : a?.alternative || _s(a);
                  const reason =
                    typeof a === "object" ? a?.reason || "" : "";
                  return (
                    <div key={i} className="border-l-2 border-red-200 pl-3">
                      <p className="text-sm text-gray-700 line-through opacity-70">
                        {alt}
                      </p>
                      {reason && (
                        <p className="text-xs text-gray-500 mt-0.5">{reason}</p>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {assumptions.length > 0 && (
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                Assumptions
              </p>
              <div className="space-y-2">
                {assumptions.map((a, i) => {
                  const text = typeof a === "string" ? a : a?.text || _s(a);
                  const evidencePath =
                    typeof a === "object" ? a?.evidence_path || "" : "";
                  // confidence is AUDIT METADATA ONLY — rendered muted, never as headline
                  const confidence =
                    typeof a === "object" && a?.confidence != null
                      ? a.confidence
                      : null;
                  return (
                    <div key={i} className="border-l-2 border-gray-200 pl-3">
                      <p className="text-sm text-gray-700">{text}</p>
                      <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                        {evidencePath && (
                          <span className="text-xs text-gray-400 font-mono">
                            {evidencePath}
                          </span>
                        )}
                        {/* confidence: muted gray tag — audit metadata only, NOT a trust headline */}
                        {confidence != null && (
                          <span className="text-xs text-gray-300 bg-gray-100 px-1.5 py-0.5 rounded font-mono">
                            conf: {typeof confidence === "number"
                              ? confidence.toFixed(2)
                              : confidence}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </CollapsibleSection>
      )}
    </div>
  );
}
