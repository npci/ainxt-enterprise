// SPDX-License-Identifier: Apache-2.0
// ============================================================
// SDLC PIPELINE DASHBOARD
// Shows active runs, stage progress, HITL approval gates,
// and per-stage agent outputs.
// ============================================================

import { useState, useEffect, useRef, memo } from "react";
import {
  GitBranch, Bug, CheckCircle2, XCircle, Clock, Loader2,
  ChevronDown, ChevronRight, Play, ThumbsUp, ThumbsDown,
  RefreshCw, ExternalLink, PlusCircle, AlertTriangle,
  BookOpen, GitPullRequest, FileText,
  RotateCcw, X as XIcon, Copy, Check, HelpCircle,
  Shield, ShieldAlert
} from "lucide-react";
import { toIST } from "../utils/time";

import { API_BASE as API, apiFetch } from '../config';
import { useToast, useConfirm } from './ui/DialogProvider.jsx';
import { useMultiRepoEnabled } from '../hooks/useMultiRepoEnabled.js';
import { validateJiraKey, validateSummary, validateRepoName, validateBranch, validateIdentifier, validateFreeText } from '../utils/securityValidation.js';
import DepTable from './DepTable.jsx';
import MultiRepoApprovalView from './MultiRepoApprovalView.jsx';
import DiffApprovalPanel from './DiffApprovalPanel.jsx';
import OpenQuestionsForm from './OpenQuestionsForm.jsx';
import WorkItemPanel from './WorkItemPanel.jsx';
import BuildMetadataApprovalPanel from './BuildMetadataApprovalPanel.jsx';
// SDLC UI redesign (2026-06-30) — backend-manifest-driven timeline + artifact-
// oriented panels. All status colours/labels/icons now live in statusModel.js
// (the single source that replaces the old hand-maintained STATE_STYLE map).
import PipelineStepper from './sdlc/PipelineStepper.jsx';
import PlanningArtifactView from './sdlc/PlanningArtifactView.jsx';
import GateSignalRow from './sdlc/GateSignalRow.jsx';
import GovernanceReviewPanel from './sdlc/GovernanceReviewPanel.jsx';
import ManifestValidationPanel from './ManifestValidationPanel.jsx';
import NavigatorActivity from './NavigatorActivity.jsx';
import { statusStyle, needsAttention } from './sdlc/statusModel.js';

// ── State badge ───────────────────────────────────────────────
// State colours/labels now live in the shared statusModel.js (the single source
// that replaced the hand-maintained STATE_STYLE map, and the FEATURE_STAGE_ORDER/
// BUG_STAGE_ORDER lists + STAGE_ICONS map that drifted from the backend). The
// timeline is rendered by <PipelineStepper> off the backend pipeline-manifest.
function badge(state) {
  const s = statusStyle(state);
  return (
      <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${s.color}`}>
      {s.label}
    </span>
  );
}

// ── RunCard ───────────────────────────────────────────────────

const _TERMINAL_STATES = new Set(["COMPLETE", "MERGED", "FAILED", "CANCELLED"]);
// COMMIT_FAILED is intentionally NOT in _TERMINAL_STATES — it is a resumable state

const RunCard = memo(function RunCard({ run, onSelect, selected, onCancelled }) {
  const isHitl = run.state === "AWAITING_CODE_APPROVAL"
      || run.state === "AWAITING_DESIGN_APPROVAL"
      || run.state === "AWAITING_SOLUTION_APPROVAL"
      || run.state === "AWAITING_PR_APPROVAL"
      || run.state === "AWAITING_RE_REVIEW"
      || run.state === "AWAITING_GOVERNANCE_APPROVAL"
      || run.state === "MERGE_CONFLICT"
      || run.state === "AWAITING_BUILD_METADATA_APPROVAL"
      || run.state === "AWAITING_USER_INPUT";
  const isQuestionsGate = run.state === "AWAITING_USER_INPUT";
  const isCommitFailed  = run.state === "COMMIT_FAILED";
  const isTerminal = _TERMINAL_STATES.has(run.state);
  const isRunning  = !isTerminal && !isHitl && !isQuestionsGate && !isCommitFailed;
  const [cancelling, setCancelling] = useState(false);
  const { confirm } = useConfirm();

  async function doCancel(e) {
    e.stopPropagation();
    const ok = await confirm({ title: "Cancel Pipeline Run", message: "Are you sure you want to cancel this pipeline run?", confirmLabel: "Yes, Cancel" });
    if (!ok) return;
    setCancelling(true);
    try {
      const resp = await apiFetch(`${API}/sdlc/runs/${run.id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "Cancelled by user", cancelled_by: "engineer" }),
      });
      if (resp.ok && onCancelled) onCancelled();
    } catch (_) {}
    finally { setCancelling(false); }
  }

// ${isHitl ? 'border-l-2 border-l-yellow-400' : ''}

  return (
      <div role='button' onClick={() => onSelect(run)}
           className={`block cursor-pointer overflow-x-hidden text-left px-4 py-3 border-b border-gray-100 rounded m-1 transition-colors
        ${selected ? `bg-indigo-50 border-l-2 ${run.type === 'bug' ? 'border-l-amber-500' : run.type === 'governance' ? 'border-l-violet-500' : 'border-l-blue-500'}` : 'hover:bg-gray-100'}
      `}>
        <div>
          <div className='flex items-center justify-between gap-2'>
            <div className='flex items-center gap-2 min-w-0'>
              {run.type === 'bug' ? (
                  <Bug size={14} className='text-red-500 flex-shrink-0' />
              ) : run.type === 'governance' ? (
                  <Shield size={14} className='text-violet-500 flex-shrink-0' />
              ) : (
                  <GitBranch size={14} className='text-indigo-500 flex-shrink-0' />
              )}
              <span
                  className={`${
                      selected ? 'text-indigo-700' : 'text-gray-600'
                  } text-sm font-medium truncate`}>
            {run.type === 'governance'
              ? `gov-${run.id.slice(0, 8)}`
              : (run.jira_key || run.id.slice(0, 8))}
          </span>
            </div>
            <div className='flex items-center gap-1.5 flex-shrink-0'>
              {isRunning && <Loader2 size={12} className='animate-spin text-blue-500' />}
              {isQuestionsGate && <HelpCircle size={12} className='text-yellow-500 animate-pulse' />}
              {isCommitFailed && <RotateCcw size={12} className='text-orange-500' title='Commit failed — click to retry' />}
              {(isRunning || run.state === "AWAITING_GOVERNANCE_APPROVAL") && (
                  <button
                      onClick={doCancel}
                      disabled={cancelling}
                      title='Cancel run'
                      className='p-1 rounded text-gray-400 hover:text-red-500 hover:bg-red-100 transition-colors disabled:opacity-40 cursor-pointer'>
                    {cancelling ? (
                        <Loader2 size={11} className='animate-spin' />
                    ) : (
                        <XIcon size={11} />
                    )}
                  </button>
              )}
            </div>
          </div>
          <div className='mt-1 flex items-center gap-2 flex-wrap'>
            {badge(run.state)}
            {run.type === 'governance' && (
              <span className='inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-violet-100 text-violet-800'>
                <Shield size={9} /> Governance
              </span>
            )}
            <span className='text-xs text-gray-400 truncate'>
          {run.jira_summary ? run.jira_summary.slice(0, 40) : '—'}
        </span>
          </div>
          {run.created_at && (
              <div className='mt-0.5 text-[10px] text-gray-400'>{toIST(run.created_at)}</div>
          )}
        </div>
      </div>
  );
});

// ── W-A: Retry-Commit Button (shown when run.state === "COMMIT_FAILED") ─

function RetryCommitButton({ run, onRetried }) {
  const [loading, setLoading] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [error, setError] = useState("");
  const { toast } = useToast();

  if (run.state !== "COMMIT_FAILED") return null;

  async function doRetry() {
    setLoading(true); setError(""); setJobId(null);
    try {
      const resp = await apiFetch(`${API}/sdlc/runs/${run.id}/retry-commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        const msg = err.detail || `Error ${resp.status}: ${resp.statusText}`;
        setError(msg);
        toast.error(msg);
        return;
      }
      const data = await resp.json();
      setJobId(data.job_id);
      toast.success("Commit & MR re-enqueued — pipeline resuming…");
      if (onRetried) onRetried();
    } catch (e) {
      const msg = e.message || "Network error";
      setError(msg);
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mt-4 rounded-lg border-2 border-orange-300 bg-orange-50 p-4">
      <div className="flex items-center gap-2 mb-2">
        <RotateCcw size={16} className="text-orange-600" />
        <span className="text-sm font-semibold text-orange-800">Commit &amp; MR Failed — Resumable</span>
      </div>
      <p className="text-xs text-orange-700 mb-3">
        The code changes are complete. Only the GitLab commit / MR creation failed
        (e.g. token expiry, network blip). Click below to retry — no code is re-generated.
      </p>
      {error && (
        <div className="mb-2 px-3 py-1.5 bg-red-50 border border-red-200 rounded text-xs text-red-700 flex items-center gap-1.5">
          <XCircle size={12} className="flex-shrink-0" /> {error}
        </div>
      )}
      {jobId && (
        <div className="mb-2 px-3 py-1.5 bg-green-50 border border-green-200 rounded text-xs text-green-700 flex items-center gap-1.5">
          <CheckCircle2 size={12} className="flex-shrink-0" />
          Re-enqueued (job: <code className="font-mono">{String(jobId).slice(0, 12)}</code>)
        </div>
      )}
      <button
        onClick={doRetry}
        disabled={loading || !!jobId}
        className="flex items-center gap-1.5 px-4 py-1.5 bg-orange-600 text-white text-xs rounded hover:bg-orange-700 disabled:opacity-50 transition-colors"
      >
        {loading
          ? <Loader2 size={13} className="animate-spin" />
          : <RotateCcw size={13} />
        }
        {loading ? "Retrying…" : jobId ? "Enqueued" : "Retry Commit & MR"}
      </button>
    </div>
  );
}

// ── Pipeline Stage Timeline ───────────────────────────────────
// The hardcoded FEATURE_STAGES / BUG_STAGES chip lists, the stageIndex() live-
// state→chip map, CHIP_TO_STAGE, and StageTimeline were REMOVED in the 2026-06-30
// UI redesign. The timeline is now rendered by <PipelineStepper> off the
// backend-owned pipeline-manifest (store/sdlc_stage_manifest.py) — a single
// source of truth that maps every live/legacy state via the manifest `aliases`,
// so the three previously-drifting frontend stage models can no longer diverge.

// ── Clarifying Q&A history (read-only) ───────────────────────
// Shows the gate questions, their options (with the recommended one badged) and
// the answer the user selected — viewable for the rest of the run's life, after
// the live AWAITING_USER_INPUT form is gone. Driven by run.context.user_answers,
// which now snapshots options + recommended alongside each answer.
function AnsweredQuestionsView({ answers }) {
  const [open, setOpen] = useState(true);
  if (!answers || !answers.length) return null;
  return (
      <div className="border border-indigo-100 rounded-lg bg-indigo-50/40">
        <button
            onClick={() => setOpen(o => !o)}
            className="w-full flex items-center justify-between px-4 py-2 text-left cursor-pointer">
          <span className="flex items-center gap-1.5 text-xs font-semibold text-indigo-800">
            <HelpCircle size={13} className="text-indigo-500" />
            Clarifying questions &amp; answers ({answers.length})
          </span>
          {open ? <ChevronDown size={14} className="text-indigo-400" />
                : <ChevronRight size={14} className="text-indigo-400" />}
        </button>
        {open && (
            <div className="px-4 pb-3 space-y-3">
              {answers.map((qa, i) => {
                const opts = qa.options || [];
                const rec = typeof qa.recommended === "number" ? qa.recommended : null;
                const sel = typeof qa.selected_option === "number" ? qa.selected_option : null;
                return (
                    <div key={i} className="text-xs">
                      <p className="font-medium text-gray-800 mb-1">{i + 1}. {qa.question}</p>
                      {opts.length > 0 && (
                          <ul className="space-y-0.5 mb-1">
                            {opts.map((opt, oi) => (
                                <li key={oi}
                                    className={`flex items-center gap-1.5 ${oi === sel ? "text-indigo-700 font-medium" : "text-gray-500"}`}>
                                  <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${oi === sel ? "bg-indigo-600" : "bg-gray-300"}`} />
                                  <span>{opt}</span>
                                  {oi === rec && <span className="px-1 rounded bg-amber-100 text-amber-700 text-[9px]">recommended</span>}
                                  {oi === sel && <span className="px-1 rounded bg-indigo-100 text-indigo-700 text-[9px]">selected</span>}
                                </li>
                            ))}
                          </ul>
                      )}
                      <p className="text-gray-600"><span className="text-gray-400">Answer:</span> {qa.answer}</p>
                      {qa.rationale && <p className="text-gray-400 italic mt-0.5">Why asked: {qa.rationale}</p>}
                    </div>
                );
              })}
            </div>
        )}
      </div>
  );
}

// ── Event Log ────────────────────────────────────────────────

function EventLog({ events }) {
  const [expanded, setExpanded] = useState(null);
  const [copiedEvId, setCopiedEvId] = useState(null);
  const { toast } = useToast();

  // Filter out bare state-transition rows emitted by _set_state() — they carry
  // no output or data and show "No details available" when expanded.
  // These are identified by actor="sdlc-state-machine" with nothing to show.
  const hasContent = ev =>
    (ev.output && ev.output.trim()) ||
    ev.data?.structured ||
    (ev.data && Object.keys(ev.data).length > 0);

  const visible = events.filter(ev =>
    ev.actor !== "sdlc-state-machine" || hasContent(ev)
  );

  const copyEvent = (ev, e) => {
    e.stopPropagation();
    const text = ev.data?.structured ?? ev.output ?? (ev.data && Object.keys(ev.data).length > 0 ? JSON.stringify(ev.data, null, 2) : "");
    if (!text) { toast.info("Nothing to copy for this event"); return; }
    navigator.clipboard.writeText(text).then(() => {
      setCopiedEvId(ev.id);
      setTimeout(() => setCopiedEvId(null), 2000);
    }).catch(() => {});
  };

  if (!visible.length) return (
      <p className="text-xs text-gray-400 py-4 text-center">No events yet.</p>
  );

  return (
      <div className="space-y-1">
        {visible.map(ev => (
            <div key={ev.id} className="border border-gray-100 rounded">
              <div className="flex items-center hover:bg-indigo-50 group">
                <button
                    onClick={() => setExpanded(expanded === ev.id ? null : ev.id)}
                    className="flex-1 text-left px-3 py-2 flex items-center gap-2 cursor-pointer min-w-0"
                >
                  <span className="text-indigo-800">{expanded === ev.id ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</span>
                  <span className="text-xs text-gray-400 font-mono w-28 flex-shrink-0">
                {toIST(ev.created_at)}
              </span>
                  <span className="text-xs text-gray-600 font-medium truncate">{ev.stage || ev.to_state}</span>
                  <span className="text-xs text-gray-400 ml-auto mr-1 flex-shrink-0">{ev.actor || "system"}</span>
                </button>
                <button
                    onClick={(e) => copyEvent(ev, e)}
                    className="flex-shrink-0 px-2 py-2 text-gray-300 hover:text-gray-600 opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer"
                    title="Copy event content"
                >
                  {copiedEvId === ev.id ? <Check size={12} className="text-green-500" /> : <Copy size={12} />}
                </button>
              </div>
              {expanded === ev.id && (
                  <div className="px-3 pb-2 text-xs text-gray-600 bg-gray-50 border-t border-gray-100">
                    {ev.data?.structured ? (
                        <pre className="whitespace-pre-wrap font-sans text-[11px] max-h-60 overflow-y-auto leading-relaxed">
                  {ev.data.structured}
                </pre>
                    ) : ev.output ? (
                        <pre className="whitespace-pre-wrap font-mono text-[11px] max-h-40 overflow-y-auto">
                  {ev.output}
                </pre>
                    ) : ev.data && Object.keys(ev.data).length > 0 ? (
                        <pre className="whitespace-pre-wrap font-mono text-[11px] max-h-40 overflow-y-auto">
                  {JSON.stringify(ev.data, null, 2)}
                </pre>
                    ) : (
                        <p className="text-gray-400 italic py-1">No details available for this event.</p>
                    )}
                  </div>
              )}
            </div>
        ))}
      </div>
  );
}

// ── W-E: Full Labeled Scope View ─────────────────────────────
// Shown at AWAITING_DESIGN_APPROVAL / AWAITING_SOLUTION_APPROVAL gates.
// Renders every file in the proposed scope, each labeled NEW vs EDIT,
// with a total count summary. Scrollable list — no truncation.

function DesignScopeView({ run }) {
  const isDesignGate   = run.state === "AWAITING_CODE_APPROVAL" || run.state === "AWAITING_DESIGN_APPROVAL";
  const isSolutionGate = run.state === "AWAITING_SOLUTION_APPROVAL";
  if (!isDesignGate && !isSolutionGate) return null;

  const ctx = run.context || {};
  // Design gate: files live in ctx.design; solution gate: ctx.fix
  const designOrFix = ctx.design || ctx.fix || {};

  // files_to_change = existing files to edit (array of strings or objects with .path / .file)
  const edits    = Array.isArray(designOrFix.files_to_change)  ? designOrFix.files_to_change  : [];
  // new_files_needed = brand-new files
  const newFiles = Array.isArray(designOrFix.new_files_needed) ? designOrFix.new_files_needed : [];

  // Also check the analysis layer (ctx.analysis) which may contain the file lists for
  // feature runs before the design object is fully populated
  const anaEdits    = Array.isArray(ctx.analysis?.files_to_change)  ? ctx.analysis.files_to_change  : [];
  const anaNewFiles = Array.isArray(ctx.analysis?.new_files_needed) ? ctx.analysis.new_files_needed : [];

  // Bug pipeline: fix uses code_changes[].file instead of files_to_change
  const codeChangeFiles = Array.isArray(ctx.fix?.code_changes)
    ? ctx.fix.code_changes.map(c => c?.file).filter(Boolean)
    : [];

  // Merge: prefer design/fix.files_to_change → analysis layer → bug code_changes
  const allEdits    = edits.length    ? edits    : (anaEdits.length    ? anaEdits    : codeChangeFiles);
  const allNewFiles = newFiles.length ? newFiles : anaNewFiles;

  const totalCount = allEdits.length + allNewFiles.length;
  if (totalCount === 0) return null;

  // Normalise a file entry to a display string
  const _label = (f) => {
    if (typeof f === "string") return f;
    return f?.path || f?.file || f?.filename || JSON.stringify(f);
  };

  return (
    <div className="mt-3 border border-yellow-200 rounded-lg bg-white overflow-hidden">
      {/* Header summary */}
      <div className="flex items-center justify-between px-3 py-2 bg-yellow-50 border-b border-yellow-200">
        <span className="text-xs font-semibold text-yellow-900">
          Proposed Scope — {totalCount} {totalCount === 1 ? "file" : "files"}
          {allEdits.length > 0 && allNewFiles.length > 0 && (
            <span className="font-normal ml-1 text-yellow-700">
              ({allEdits.length} edit{allEdits.length !== 1 ? "s" : ""}, {allNewFiles.length} new)
            </span>
          )}
          {allEdits.length > 0 && allNewFiles.length === 0 && (
            <span className="font-normal ml-1 text-yellow-700">
              ({allEdits.length} edit{allEdits.length !== 1 ? "s" : ""})
            </span>
          )}
          {allEdits.length === 0 && allNewFiles.length > 0 && (
            <span className="font-normal ml-1 text-yellow-700">
              ({allNewFiles.length} new)
            </span>
          )}
        </span>
        <span className="text-[10px] text-yellow-600">Approving = approving this full scope</span>
      </div>

      {/* Scrollable file list — no truncation */}
      <ul className="max-h-64 overflow-y-auto divide-y divide-gray-50 px-0 py-0">
        {allEdits.map((f, i) => (
          <li key={`edit-${i}`} className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-50">
            <span className="flex-shrink-0 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-blue-100 text-blue-700 uppercase tracking-wide">
              EDIT
            </span>
            <span className="font-mono text-[11px] text-gray-700 break-all">{_label(f)}</span>
          </li>
        ))}
        {allNewFiles.map((f, i) => (
          <li key={`new-${i}`} className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-50">
            <span className="flex-shrink-0 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-emerald-100 text-emerald-700 uppercase tracking-wide">
              NEW
            </span>
            <span className="font-mono text-[11px] text-gray-700 break-all">{_label(f)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── HITL Approval Panel ───────────────────────────────────────

function ApprovalPanel({ run, onActionDone, user }) {
  const ctx = run.context || {};
  const [feedback, setFeedback] = useState("");
  const [rejectReason, setRejectReason] = useState("");
  const [revisionFeedback, setRevisionFeedback] = useState("");
  const [loading, setLoading] = useState(false);
  const [govLoading, setGovLoading] = useState(false);
  const [mode, setMode] = useState(null); // "approve" | "reject" | "revise" | "cancel"
  const [apiError, setApiError] = useState("");
  const [fieldErrors, setFieldErrors] = useState({ feedback: "", rejectReason: "", revisionFeedback: "" });
  const { confirm } = useConfirm();
  // Per-file request-changes comments (2026-07-29), collected by DiffApprovalPanel
  // and bubbled up here so they can ride along with the whole-run `feedback` on
  // POST /runs/{id}/request-changes. Array of { file, line, comment }.
  const [fileComments, setFileComments] = useState([]);
  // Resume-time skip_tests toggle — shown at design/solution HITL gates so the
  // engineer can opt-out of tests+SLT without re-triggering the whole pipeline.
  // Initialised from the stored run context so the checkbox reflects the trigger-time choice.
  const [skipTestsOverride, setSkipTestsOverride] = useState(
    run.context?.skip_tests === true
  );
  // Resume-time skip_compile toggle — shown ONLY when the post-apply build FAILED
  // (run.context.build_failed set by the state machine). Ticking it +approving sends
  // skip_compile_override, which sets compile_skipped so the post-gate machine
  // re-applies the existing diff, skips the build, and pushes the already-created code.
  const [skipCompileOverride, setSkipCompileOverride] = useState(false);
  // Re-sync when the run changes (e.g. after polling updates the run object)
  useEffect(() => {
    setSkipTestsOverride(run.context?.skip_tests === true);
    setSkipCompileOverride(false);
  }, [run.id]);
  // A post-apply build failure parks the run at the code/solution approval gate with
  // a build_failed marker so this panel can render the failure + the skip action.
  const buildFailed = !!(run.context?.build_failed);
  const buildFailedReason =
    (run.context?.build_failed && run.context.build_failed.reason)
    || run.context?.suspend_reason
    || "post-gate build failed";

  const isHitl = run.state === "AWAITING_CODE_APPROVAL"
      || run.state === "AWAITING_DESIGN_APPROVAL"
      || run.state === "AWAITING_SOLUTION_APPROVAL"
      || run.state === "AWAITING_PR_APPROVAL"
      || run.state === "AWAITING_RE_REVIEW"
      || run.state === "MERGE_CONFLICT";

  // Open-questions gate (question_answer_required) — raised via
  // AWAITING_USER_INPUT, disambiguated by ctx.gate_kind. Separate from isHitl
  // because the action is "submit answers", not approve/reject.
  //   gate_kind="normalization" → GATE 1 (WorkItemPanel, always fires post-NORMALIZE)
  //   gate_kind="questions" (or absent) → GATE 2 (OpenQuestionsForm, classify-raised)
  const isQuestionsGate = run.state === "AWAITING_USER_INPUT";
  const isNormalizationGate = isQuestionsGate && ctx.gate_kind === "normalization";

  // Build-metadata gate (Issue 1) — base-branch-detected language version differs
  // from the stored (product, repo) metadata; the operator confirms which to use.
  const isBuildMetadataGate = run.state === "AWAITING_BUILD_METADATA_APPROVAL";

  const isTerminal = _TERMINAL_STATES.has(run.state);

  // Show cancel button for all non-terminal states (including HITL)
  const canCancel = !isTerminal;
  // Request Changes only at design/solution HITL gates (not PR gates)
  const canRequestChanges = run.state === "AWAITING_CODE_APPROVAL"
      || run.state === "AWAITING_DESIGN_APPROVAL"
      || run.state === "AWAITING_SOLUTION_APPROVAL";
  // Per-file request-changes control (2026-07-29) is additionally available at the
  // PR-approval gate — see backend contract on POST /runs/{id}/request-changes
  // `file_comments`. Kept separate from `canRequestChanges` above so gate-specific
  // UI (e.g. the skip-tests override, which stays design/solution-only) is unaffected.
  const canRequestChangesHere = canRequestChanges || run.state === "AWAITING_PR_APPROVAL";

  const revisionCount = run.context?.revision_count || 0;
  const revisionsLeft = Math.max(0, 3 - revisionCount);

  if (!isHitl && !isQuestionsGate && !isBuildMetadataGate && !canCancel) return null;

  const isPrGate        = run.state === "AWAITING_PR_APPROVAL";
  const isReReview      = run.state === "AWAITING_RE_REVIEW";
  const isMergeConflict = run.state === "MERGE_CONFLICT";

  // Owner/admin gate for "Send to Governance" — mirrors the backend's
  // _is_run_owner() (routers/sdlc_router.py POST /governance/start): admin, or
  // the user who triggered this run, matched by email/user-id against
  // context.triggered_by_email / context.triggered_by_user_id / run.created_by.
  const isAdminUser = user?.role === "admin";
  const _curEmail   = (user?.email || "").trim().toLowerCase();
  const _trigEmail  = (ctx.triggered_by_email || "").trim().toLowerCase();
  const _curIds     = [user?.sub, user?.id, user?.email].filter(Boolean).map(String);
  const _createdBy  = String(run.created_by || "");
  const _trigUid    = String(ctx.triggered_by_user_id || "");
  const isRunOwner  = (!!_curEmail && !!_trigEmail && _curEmail === _trigEmail)
      || (!!_createdBy && _curIds.includes(_createdBy))
      || (!!_trigUid && _curIds.includes(_trigUid));
  const canSendToGovernance = isPrGate && run.type !== "governance" && (isAdminUser || isRunOwner);
  const label = isMergeConflict
      ? "Merge conflict detected — review resolution proposal and resolve manually"
      : isPrGate
          ? "PR is ready for merge"
          : isReReview
              ? "AI has addressed review comments — awaiting re-review"
              : "Design / solution is ready for coding";

  function validateHitlField(fieldName, value) {
    if (!value || !value.trim()) {
      if (fieldName === "rejectReason") return "Rejection reason is required";
      if (fieldName === "revisionFeedback") return "Revision feedback is required";
      return ""; // feedback is optional for approve
    }
    const result = validateSummary(value);
    return result.isValid ? "" : (result.errors[0]?.message || "Invalid input");
  }

  function handleHitlChange(fieldName, value, setter) {
    setter(value);
    if (fieldErrors[fieldName]) setFieldErrors(prev => ({ ...prev, [fieldName]: "" }));
  }

  async function doApprove() {
    // feedback is optional — only validate if provided
    const feedbackErr = validateHitlField("feedback", feedback);
    if (feedbackErr) { setFieldErrors(prev => ({ ...prev, feedback: feedbackErr })); return; }
    setLoading(true); setApiError("");
    try {
      const resp = await apiFetch(`${API}/sdlc/runs/${run.id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          feedback,
          approved_by: "engineer",
          // Send the override only at design/solution gates; null at other gates.
          skip_tests_override: (canRequestChanges ? skipTestsOverride : null),
          // 'Skip compilation & continue' — only meaningful at a post-apply build
          // failure (buildFailed); null otherwise so normal approvals are unchanged.
          skip_compile_override: (canRequestChanges && buildFailed ? skipCompileOverride : null),
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setApiError(err.detail || `Error ${resp.status}: ${resp.statusText}`);
        return;
      }
      setMode(null); setFeedback(""); setApiError(""); setFieldErrors({ feedback: "", rejectReason: "", revisionFeedback: "" });
      onActionDone();
    } finally { setLoading(false); }
  }

  async function doReject() {
    const reasonErr = validateHitlField("rejectReason", rejectReason);
    if (reasonErr) { setFieldErrors(prev => ({ ...prev, rejectReason: reasonErr })); return; }
    setLoading(true); setApiError("");
    try {
      const resp = await apiFetch(`${API}/sdlc/runs/${run.id}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: rejectReason, rejected_by: "engineer" }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setApiError(err.detail || `Error ${resp.status}: ${resp.statusText}`);
        return;
      }
      setMode(null); setRejectReason(""); setApiError(""); setFieldErrors({ feedback: "", rejectReason: "", revisionFeedback: "" });
      onActionDone();
    } finally { setLoading(false); }
  }

  async function doRequestChanges() {
    const revErr = validateHitlField("revisionFeedback", revisionFeedback);
    if (revErr) { setFieldErrors(prev => ({ ...prev, revisionFeedback: revErr })); return; }
    // Submit when EITHER whole-run feedback OR at least one per-file comment is
    // present — the backend accepts file-comments-only (feedback is optional).
    if (!revisionFeedback.trim() && fileComments.length === 0) return;
    setLoading(true); setApiError("");
    try {
      const resp = await apiFetch(`${API}/sdlc/runs/${run.id}/request-changes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          feedback: revisionFeedback,
          revised_by: "engineer",
          file_comments: fileComments.length ? fileComments : undefined,
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setApiError(err.detail || `Error ${resp.status}: ${resp.statusText}`);
        return;
      }
      setMode(null); setRevisionFeedback(""); setApiError(""); setFieldErrors({ feedback: "", rejectReason: "", revisionFeedback: "" });
      onActionDone();
    } finally { setLoading(false); }
  }

  async function doCancel() {
    setLoading(true); setApiError("");
    try {
      const resp = await apiFetch(`${API}/sdlc/runs/${run.id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "Cancelled by engineer", cancelled_by: "engineer" }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setApiError(err.detail || `Error ${resp.status}: ${resp.statusText}`);
        return;
      }
      setMode(null); setApiError("");
      onActionDone();
    } finally { setLoading(false); }
  }

  // Author-initiated governance end-gate (2026-07-24): the MR is opened at commit
  // as a normal non-draft MR and the run sits at AWAITING_PR_APPROVAL; the owner/
  // admin clicks this to run governance before merge. On success the run moves to
  // GOVERNANCE_SCAN, then either back to AWAITING_PR_APPROVAL (clean) or suspends
  // at AWAITING_GOVERNANCE_APPROVAL (blocking findings) — both already styled in
  // statusModel.js, so a plain refresh via onActionDone is enough here.
  async function doSendToGovernance() {
    setGovLoading(true); setApiError("");
    try {
      const resp = await apiFetch(`${API}/sdlc/runs/${run.id}/governance/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setApiError(err.detail || `Error ${resp.status}: ${resp.statusText}`);
        return;
      }
      setApiError("");
      onActionDone();
    } finally { setGovLoading(false); }
  }

  return (
      <div className={`mt-4 rounded-lg border-2 p-4 ${isMergeConflict ? "border-red-400 bg-red-50" : "border-yellow-300 bg-yellow-50"}`}>
        {isMergeConflict && (
            <div className="mb-3">
              <div className="flex items-center gap-2 mb-2">
                <AlertTriangle size={16} className="text-red-600" />
                <span className="text-sm font-semibold text-red-800">Merge Conflict Detected</span>
              </div>
              <p className="text-xs text-red-700 mb-3">{label}</p>
              {run.branch && (
                  <div className="flex items-center gap-1.5 text-xs text-red-700 mb-2">
                    <GitBranch size={11} />
                    <span>Branch <code className="font-mono font-semibold">{run.branch}</code> has conflicts with the base branch</span>
                  </div>
              )}
              {run.pr_url && (
                  <div className="mb-3">
                    <a href={run.pr_url} target="_blank" rel="noopener noreferrer"
                       className="text-xs text-indigo-600 hover:underline font-medium flex items-center gap-0.5">
                      <GitPullRequest size={10} /> View MR on GitLab <ExternalLink size={9} />
                    </a>
                  </div>
              )}
              <p className="text-xs text-red-700 font-medium mb-1">Review resolution proposal in Inbox, then resolve manually and re-trigger.</p>
              {run.context?.resolution_proposal && (
                  <div className="mt-2">
                    <p className="text-xs font-semibold text-red-800 mb-1">AI Resolution Proposal:</p>
                    <pre className="text-xs bg-white border border-red-200 rounded p-2 overflow-y-auto whitespace-pre-wrap max-h-48 leading-relaxed text-gray-700">
                {run.context.resolution_proposal}
              </pre>
                  </div>
              )}
            </div>
        )}
        {isHitl && !isMergeConflict && (
            <>
              <div className="flex items-center justify-between gap-2 mb-2">
                <div className="flex items-center gap-2">
                  <AlertTriangle size={16} className="text-yellow-600" />
                  <span className="text-sm font-semibold text-yellow-800">Human Approval Required</span>
                </div>
                {/* ENH-8: Waiting-since indicator */}
                {run.updated_at && (
                  <span className="text-[10px] text-yellow-600 flex-shrink-0">
                    Waiting since {toIST(run.updated_at)}
                  </span>
                )}
              </div>
              <p className="text-xs text-yellow-700 mb-2">{label}</p>
              <MultiRepoApprovalView repos={run.context?.repos} />
              {/* Trust-calibrated signal row (design/solution gates only): leads with
                  deterministic coverage + grounding + MANIFEST_VALIDATION (primary),
                  keeps model confidence/consistency visually secondary (Research Q2,
                  confidence ≤15%). Shown above the verified diff so the reviewer sees
                  *why* it's ready. */}
              {canRequestChanges && (
                <GateSignalRow run={run} runId={run.id} runType={run.type} />
              )}
              {/* W-E: Full labeled scope — shown for design/solution gates */}
              <DesignScopeView run={run} />
              {/* Shift-left: the human approves the REAL compiled+tested diff.
                  onFileCommentsChange bubbles the per-file request-changes comments
                  (2026-07-29) up so doRequestChanges() can POST them alongside the
                  whole-run feedback. */}
              <DiffApprovalPanel run={run} onFileCommentsChange={setFileComments} />
            </>
        )}

        {isQuestionsGate && (
            isNormalizationGate ? (
              <WorkItemPanel
                  runId={run.id}
                  workItem={ctx.work_item || {}}
                  questions={ctx.pending_questions || []}
                  onSubmitted={() => {
                    if (typeof onActionDone === "function") onActionDone();
                  }}
              />
            ) : (
              <OpenQuestionsForm
                  runId={run.id}
                  questions={ctx.pending_questions || []}
                  onSubmitted={() => {
                    // ApprovalPanel receives `onActionDone` from the parent to refresh
                    // the list/detail after an approval. Reuse it here so the form
                    // submission triggers the same "show me the new state" refresh.
                    if (typeof onActionDone === "function") onActionDone();
                  }}
              />
            )
        )}

        {isBuildMetadataGate && (
            <BuildMetadataApprovalPanel
                runId={run.id}
                gate={ctx.build_metadata_gate || {}}
                onSubmitted={() => {
                  if (typeof onActionDone === "function") onActionDone();
                }}
            />
        )}

        {/* API error feedback */}
        {apiError && (
            <div className="mb-2 px-3 py-1.5 bg-red-50 border border-red-200 rounded text-xs text-red-700 flex items-center gap-1.5">
              <XCircle size={12} className="flex-shrink-0" /> {apiError}
            </div>
        )}

        {/* Post-apply BUILD FAILURE notice — the state machine parked the run here
            because compilation failed after applying the approved diff. The engineer
            can push anyway via 'Skip compilation & continue' (the checkbox in the
            Approve panel). */}
        {buildFailed && (
            <div className="mb-2 px-3 py-1.5 bg-red-50 border border-red-200 rounded text-xs text-red-700 flex items-start gap-1.5">
              <XCircle size={12} className="flex-shrink-0 mt-0.5" />
              <span>
                <span className="font-medium">Compilation failed.</span>{" "}
                {buildFailedReason}. The code was applied but did not build. Choose
                <span className="font-medium"> Approve</span> and tick
                <span className="font-medium"> “Skip compilation &amp; continue”</span> to
                push the created code without a successful build, or
                <span className="font-medium"> Request Changes</span> to fix it.
              </span>
            </div>
        )}

        {/* PR gate details */}
        {isPrGate && (run.branch || run.pr_url) && (
            <div className="mb-3 p-2 bg-white border border-yellow-200 rounded text-xs space-y-1">
              {run.branch && (
                  <div className="flex items-center gap-1.5 text-gray-700">
                    <GitBranch size={11} className="text-gray-400" />
                    <span className="font-medium">Branch:</span>
                    <code className="font-mono text-indigo-600">{run.branch}</code>
                  </div>
              )}
              {run.pr_url && (
                  <div className="flex items-center gap-1.5">
                    <GitPullRequest size={11} className="text-gray-400" />
                    <a href={run.pr_url} target="_blank" rel="noopener noreferrer"
                       className="text-indigo-600 hover:underline font-medium flex items-center gap-0.5">
                      View MR on GitLab <ExternalLink size={9} />
                    </a>
                  </div>
              )}
              {run.type !== "governance" && (run.context?.jira_url || run.jira_key) && (
                  <div className="flex items-center gap-1.5">
                    <ExternalLink size={11} className="text-gray-400" />
                    <a href={run.context?.jira_url || `#`} target="_blank" rel="noopener noreferrer"
                       className="text-orange-600 hover:underline font-medium flex items-center gap-0.5">
                      Jira: {run.jira_key} <ExternalLink size={9} />
                    </a>
                  </div>
              )}
            </div>
        )}

        {!mode && (
            <div className="flex flex-wrap gap-2">
              {isHitl && (
                  <button
                      onClick={() => { setMode("approve"); setFieldErrors({ feedback: "", rejectReason: "", revisionFeedback: "" }); }}
                      className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white text-xs rounded hover:bg-green-700"
                  >
                    <ThumbsUp size={12} /> Approve
                  </button>
              )}
              {canSendToGovernance && (
                  <button
                      onClick={doSendToGovernance}
                      disabled={govLoading}
                      title="Run the pre-merge governance end-gate against this MR"
                      className="flex items-center gap-1 px-3 py-1.5 bg-violet-600 text-white text-xs rounded hover:bg-violet-700 disabled:opacity-50"
                  >
                    {govLoading ? <Loader2 size={12} className="animate-spin" /> : <Shield size={12} />}
                    Send to Governance
                  </button>
              )}
              {/* PR-approval gate is UNCAPPED (matches the backend + PR webhook path);
                  the 3-revision cap applies only to the pre-apply code/solution gate. */}
              {canRequestChangesHere && (run.state === "AWAITING_PR_APPROVAL" || revisionsLeft > 0) && (
                  <button
                      onClick={() => { setMode("revise"); setFieldErrors({ feedback: "", rejectReason: "",
                          revisionFeedback: "" }); }}
                      className="flex items-center gap-1 px-3 py-1.5 bg-amber-500 text-white text-xs rounded hover:bg-amber-600"
                      title={run.state === "AWAITING_PR_APPROVAL"
                          ? "Request Changes on the MR (uncapped)"
                          : `Request Changes (${revisionsLeft} of 3 remaining)`}
                  >
                    <RotateCcw size={12} /> Request Changes
                    {run.state !== "AWAITING_PR_APPROVAL" && (
                      <span className="ml-1 opacity-70 text-[10px]">({revisionCount}/3)</span>
                    )}
                  </button>
              )}
              {isHitl && (
                  <button
                      onClick={() => { setMode("reject"); setFieldErrors({ feedback: "", rejectReason: "", revisionFeedback: "" }); }}
                      title="Permanently terminate this run — moves to FAILED state with no recovery"
                      className="flex items-center gap-1 px-3 py-1.5 bg-red-600 text-white text-xs rounded hover:bg-red-700"
                  >
                    <ThumbsDown size={12} /> Reject (Terminate)
                  </button>
              )}
              {canCancel && (
                  <button
                      onClick={() => setMode("cancel")}
                      className="flex items-center gap-1 px-3 py-1.5 bg-gray-400 text-white text-xs rounded hover:bg-gray-500"
                  >
                    <XIcon size={12} /> Cancel Pipeline
                  </button>
              )}
            </div>
        )}

        {mode === "approve" && (
            <div className="mt-2 space-y-2">
          <textarea
              className={`w-full border rounded p-2 text-xs resize-none focus:outline-none focus:ring-1 ${fieldErrors.feedback ? "border-red-400 focus:ring-red-400" : "border-yellow-300 focus:ring-yellow-400"}`}
              rows={2}
              placeholder="Optional: feedback for the coding agent..."
              value={feedback}
              maxLength={500}
              onChange={e => handleHitlChange("feedback", e.target.value, setFeedback)}
          />
          <div className="flex items-center justify-between">
            {fieldErrors.feedback ? <p className="text-xs text-red-500">{fieldErrors.feedback}</p> : <span />}
            <span className={`text-[10px] ${feedback.length > 450 ? "text-amber-500" : "text-gray-400"}`}>{feedback.length}/500</span>
          </div>
              {/* Resume-time skip_tests toggle — only at design/solution gates */}
              {canRequestChanges && (
                <div className="border border-yellow-200 rounded p-2 bg-yellow-50/60">
                  <label className="flex items-center gap-2 cursor-pointer group">
                    <input
                      type="checkbox"
                      checked={skipTestsOverride}
                      onChange={e => setSkipTestsOverride(e.target.checked)}
                      className="w-3.5 h-3.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-400"
                    />
                    <span className="text-xs text-gray-700 group-hover:text-gray-900">Skip Tests + SLT for this run</span>
                    <span className="text-[10px] text-amber-600">(PCI/DSS: keep enabled unless explicitly waived)</span>
                  </label>
                </div>
              )}
              {/* Skip-compilation toggle — ONLY when the post-apply build failed.
                  Ticking it +approving re-applies the existing diff, skips the build,
                  and pushes the created code to remote (compile_skipped). */}
              {canRequestChanges && buildFailed && (
                <div className="border border-red-200 rounded p-2 bg-red-50/60">
                  <label className="flex items-center gap-2 cursor-pointer group">
                    <input
                      type="checkbox"
                      checked={skipCompileOverride}
                      onChange={e => setSkipCompileOverride(e.target.checked)}
                      className="w-3.5 h-3.5 rounded border-gray-300 text-red-600 focus:ring-red-400"
                    />
                    <span className="text-xs text-gray-700 group-hover:text-gray-900">Skip compilation &amp; continue</span>
                    <span className="text-[10px] text-red-600">(pushes the code WITHOUT a successful build)</span>
                  </label>
                </div>
              )}
              <div className="flex gap-2">
                <button
                    onClick={doApprove}
                    disabled={loading}
                    className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white text-xs rounded hover:bg-green-700 disabled:opacity-50"
                >
                  {loading ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
                  Confirm Approval
                </button>
                <button onClick={() => setMode(null)} className="text-xs text-gray-500 hover:text-gray-700">
                  Back
                </button>
              </div>
            </div>
        )}

        {mode === "revise" && (
            <div className="mt-2 space-y-2">
              <p className="text-xs text-amber-700 font-medium">
                {/* NOTE: Request Changes is now also reachable at AWAITING_PR_APPROVAL
                    (see canRequestChangesHere) — copy adapted so the PR-gate case reads
                    correctly since "design" doesn't apply there. */}
                {run.state === "AWAITING_PR_APPROVAL"
                    ? `The AI will address these PR comments and return for re-approval.`
                    : `Revision ${revisionCount + 1} of 3 — the AI will revise the design and return for re-approval.`}
              </p>
              <textarea
                  className={`w-full border rounded p-2 text-xs resize-none focus:outline-none focus:ring-1 ${fieldErrors.revisionFeedback ? "border-red-400 focus:ring-red-400" : "border-amber-300 focus:ring-amber-400"}`}
                  rows={3}
                  placeholder="Describe what needs to change (or add per-file comments on the diff)..."
                  value={revisionFeedback}
                  maxLength={500}
                  onChange={e => handleHitlChange("revisionFeedback", e.target.value, setRevisionFeedback)}
              />
              <div className="flex items-center justify-between">
                {fieldErrors.revisionFeedback ? <p className="text-xs text-red-500">{fieldErrors.revisionFeedback}</p> : <span />}
                <span className={`text-[10px] ${revisionFeedback.length > 450 ? "text-amber-500" : "text-gray-400"}`}>{revisionFeedback.length}/500</span>
              </div>
              <div className="flex gap-2">
                <button
                    onClick={doRequestChanges}
                    disabled={loading || (!revisionFeedback.trim() && fileComments.length === 0)}
                    className="flex items-center gap-1 px-3 py-1.5 bg-amber-500 text-white text-xs rounded hover:bg-amber-600 disabled:opacity-50"
                >
                  {loading ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />}
                  Submit Changes
                </button>
                <button onClick={() => setMode(null)} className="text-xs text-gray-500 hover:text-gray-700">
                  Back
                </button>
              </div>
            </div>
        )}

        {mode === "reject" && (
            <div className="mt-2 space-y-2">
              <p className="text-xs text-red-700 font-medium">This will permanently terminate the run (FAILED).</p>
              <textarea
                  className={`w-full border rounded p-2 text-xs resize-none focus:outline-none focus:ring-1 ${fieldErrors.rejectReason ? "border-red-500 focus:ring-red-500" : "border-red-300 focus:ring-red-400"}`}
                  rows={2}
                  placeholder="Reason for rejection (required)..."
                  value={rejectReason}
                  maxLength={500}
                  onChange={e => handleHitlChange("rejectReason", e.target.value, setRejectReason)}
              />
              <div className="flex items-center justify-between">
                {fieldErrors.rejectReason ? <p className="text-xs text-red-500">{fieldErrors.rejectReason}</p> : <span />}
                <span className={`text-[10px] ${rejectReason.length > 450 ? "text-amber-500" : "text-gray-400"}`}>{rejectReason.length}/500</span>
              </div>
              <div className="flex gap-2">
                <button
                    onClick={doReject}
                    disabled={loading || !rejectReason.trim()}
                    className="flex items-center gap-1 px-3 py-1.5 bg-red-600 text-white text-xs rounded hover:bg-red-700 disabled:opacity-50"
                >
                  {loading ? <Loader2 size={12} className="animate-spin" /> : <XCircle size={12} />}
                  Confirm Rejection
                </button>
                <button onClick={() => setMode(null)} className="text-xs text-gray-500 hover:text-gray-700">
                  Back
                </button>
              </div>
            </div>
        )}

        {mode === "cancel" && (
            <div className="mt-2 space-y-2">
              <p className="text-xs text-gray-700">Cancel this pipeline run? The run will be marked Cancelled.</p>
              <div className="flex gap-2">
                <button
                    onClick={doCancel}
                    disabled={loading}
                    className="flex items-center gap-1 px-3 py-1.5 bg-gray-500 text-white text-xs rounded hover:bg-gray-600 disabled:opacity-50"
                >
                  {loading ? <Loader2 size={12} className="animate-spin" /> : <XIcon size={12} />}
                  Confirm Cancel
                </button>
                <button onClick={() => setMode(null)} className="text-xs text-gray-500 hover:text-gray-700">
                  Back
                </button>
              </div>
            </div>
        )}
      </div>
  );
}

// ── Trigger Modal ─────────────────────────────────────────────

const LANGUAGE_OPTIONS = [
  { value: "", label: "Auto-detect (from GitLab / indexed files)" },
  { value: "java",       label: "Java" },
  { value: "kotlin",     label: "Kotlin" },
  { value: "python",     label: "Python" },
  { value: "javascript", label: "JavaScript / Node.js" },
  { value: "typescript", label: "TypeScript" },
  { value: "go",         label: "Go" },
  { value: "csharp",     label: "C#" },
  { value: "scala",      label: "Scala" },
  { value: "ruby",       label: "Ruby" },
];

function TriggerModal({ onClose, onTriggered, defaults = null }) {
  const [type, setType] = useState(defaults?.type || "feature");
  const [jiraKey, setJiraKey] = useState(defaults?.jira_key || "");
  const [summary, setSummary] = useState(defaults?.summary || "");
  const [jiraDesc, setJiraDesc] = useState("");
  const [repo, setRepo] = useState(defaults?.repo || "");
  const [branch, setBranch] = useState(defaults?.branch || "");
  const [branchOverridden, setBranchOverridden] = useState(false);
  const [langOverride, setLangOverride] = useState("");
  const [runTests, setRunTests] = useState(false);
  const [runSlt, setRunSlt] = useState(false);
  // Governance review (EA/IS/DPDP pluggable skills over the diff) — opt-in trigger.
  // NOTE: there is no backend-exposed catalog of governance skill slugs today, so
  // the subset picker is a free-form comma-separated input (empty = all loaded
  // skills). See agents/sdlc_governance/config.py::parse_subset.
  const [runGovernanceReview, setRunGovernanceReview] = useState(false);
  // Governance-only trigger fields
  const [govBaseBranch, setGovBaseBranch] = useState("main");
  const [govBaseCommit, setGovBaseCommit] = useState("");
  const [govHeadBranch, setGovHeadBranch] = useState("");
  const [loading, setLoading] = useState(false);
  const [jiraLoading, setJiraLoading] = useState(false);
  const [jiraMsg, setJiraMsg] = useState(null); // { type: "ok"|"warn", text }
  const [error, setError] = useState("");
  const [deps, setDeps] = useState([]);

  const multiRepoEnabled = useMultiRepoEnabled();

  const EMPTY_ERRORS = { jiraKey: "", summary: "", repo: "", branch: "" };
  const [formErrors, setFormErrors] = useState(EMPTY_ERRORS);

  function validateField(fieldName, value) {
    if (fieldName === "jiraKey" && (!value || !value.trim())) return "Jira key is required";
    if (fieldName === "summary" && (!value || !value.trim())) return "Summary is required";
    switch (fieldName) {
      case "jiraKey": {
        const r = validateJiraKey(value);
        return r.isValid ? "" : (r.errors[0]?.message || "Invalid Jira key");
      }
      case "summary": {
        const r = validateSummary(value);
        return r.isValid ? "" : (r.errors[0]?.message || "Invalid summary");
      }
      case "repo": {
        if (!value.trim()) return "";
        const r = validateRepoName(value);
        return r.isValid ? "" : (r.errors[0]?.message || "Invalid repository");
      }
      case "branch": {
        const r = validateBranch(value);
        return r.isValid ? "" : (r.errors[0]?.message || "Invalid branch");
      }
      default:
        return "";
    }
  }

  function handleBlur(fieldName, value) {
    const err = validateField(fieldName, value);
    setFormErrors(prev => ({ ...prev, [fieldName]: err }));
  }

  function handleChange(fieldName, value, setter) {
    setter(value);
    if (formErrors[fieldName]) {
      setFormErrors(prev => ({ ...prev, [fieldName]: "" }));
    }
  }

  // Product → Repo cascade
  const [products, setProducts] = useState([]);
  const [selectedProductId, setSelectedProductId] = useState("");
  const [productRepos, setProductRepos] = useState([]); // [{repo, branch}]
  const [productsLoading, setProductsLoading] = useState(false);
  const [reposLoading, setReposLoading] = useState(false);

  // Hydrate form fields from `defaults` (e.g. re-trigger prefill). Guarded so an
  // empty/undefined defaults never clobbers user input, and runs once per
  // `defaults` change so it doesn't fight with subsequent user edits.
  useEffect(() => {
    if (!defaults) return;
    if (defaults.product_id) setSelectedProductId(defaults.product_id);
    if (Array.isArray(defaults.dependencies) && defaults.dependencies.length > 0) {
      setDeps(defaults.dependencies.map(d => ({ repo: d.repo || "", ref: d.ref || "", kind: d.kind || "" })));
    }
    if (defaults.base_branch) setGovBaseBranch(defaults.base_branch);
    if (defaults.base_commit) setGovBaseCommit(defaults.base_commit);
    if (defaults.head_branch) setGovHeadBranch(defaults.head_branch);
  }, [defaults]);

  // Close modal on Escape key
  useEffect(() => {
    const handler = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  const _jiraDebounceRef = useRef(null);
  // Latest key the field holds — used to discard out-of-order/stale autofill
  // responses (e.g. an in-flight fetch for partial "NES-142" must not overwrite
  // fields once the user has finished typing "NES-1427").
  const _jiraLatestKeyRef = useRef("");

  // Load products on mount
  useEffect(() => {
    setProductsLoading(true);
    apiFetch(`${API}/sdlc/products`)
        .then(r => r.json())
        .then(d => setProducts(d.products || []))
        .catch(() => {})
        .finally(() => setProductsLoading(false));
  }, []);

  // When product changes → load repos
  useEffect(() => {
    if (!selectedProductId) { setProductRepos([]); return; }
    setReposLoading(true);
    apiFetch(`${API}/sdlc/products/${selectedProductId}/repos`)
        .then(r => r.json())
        .then(d => setProductRepos(d.repos || []))
        .catch(() => setProductRepos([]))
        .finally(() => setReposLoading(false));
  }, [selectedProductId]);

  // When repo selected from product dropdown → auto-fill branch
  function onProductRepoChange(repoName) {
    setRepo(repoName);
    if (!branchOverridden) {
      const found = productRepos.find(r => r.repo === repoName);
      if (found) setBranch(found.branch || "");
    }
  }

  // When product changes → reset repo + branch
  function onProductChange(pid) {
    setSelectedProductId(pid);
    setRepo("");
    if (!branchOverridden) setBranch("");
  }

  async function fetchJiraDetails(key) {
    if (!key || !/^[A-Z][a-zA-Z0-9]*-\d+$/.test(key)) return;
    setJiraLoading(true);
    setJiraMsg(null);
    try {
      const r = await apiFetch(`${API}/sdlc/jira-ticket/${key}`);
      // Discard a stale/out-of-order response: the field has moved on to a
      // different key since this fetch was issued (race while typing).
      if (key !== _jiraLatestKeyRef.current) return;
      if (!r.ok) throw new Error("not found");
      const d = await r.json();
      if (key !== _jiraLatestKeyRef.current) return;
      if (d.summary) setSummary(d.summary);
      if (d.description) setJiraDesc(d.description.slice(0, 300));
      setJiraMsg({ type: "ok", text: "Details fetched from JIRA" });
    } catch {
      if (key !== _jiraLatestKeyRef.current) return;
      setJiraMsg({ type: "warn", text: "Could not fetch from JIRA — type summary manually" });
    } finally {
      if (key === _jiraLatestKeyRef.current) setJiraLoading(false);
    }
  }

  function onJiraKeyChange(val) {
    setJiraKey(val);
    setJiraMsg(null);
    const normKey = val.trim().toUpperCase();
    _jiraLatestKeyRef.current = normKey;  // synchronous — so in-flight fetches can detect staleness
    if (_jiraDebounceRef.current) clearTimeout(_jiraDebounceRef.current);
    _jiraDebounceRef.current = setTimeout(() => fetchJiraDetails(normKey), 600);
  }

  async function submit() {
    // Governance type: separate validation + endpoint
    if (type === "governance") {
      if (!repo.trim()) {
        setError("Repository is required.");
        return;
      }
      if (!govHeadBranch.trim()) {
        setError("Head branch is required.");
        return;
      }

      // Client-side pre-check — mirrors validate_governance_trigger_request()
      // in core/security_validation.py: repo/head_branch/base_branch all go
      // through validate_identifier() there (they feed git clone/checkout
      // commands), not validate_free_text(). The backend (POST /sdlc/governance)
      // remains the authoritative enforcer.
      const repoCheck = validateIdentifier(repo);
      if (!repoCheck.isValid) { setError(repoCheck.errors[0]?.message || "Invalid repository"); return; }
      const headBranchCheck = validateIdentifier(govHeadBranch);
      if (!headBranchCheck.isValid) { setError(headBranchCheck.errors[0]?.message || "Invalid head branch"); return; }
      const baseBranchCheck = validateIdentifier(govBaseBranch || "main");
      if (!baseBranchCheck.isValid) { setError(baseBranchCheck.errors[0]?.message || "Invalid base branch"); return; }

      setLoading(true); setError("");
      try {
        const payload = {
          product_id: selectedProductId || null,
          repo: repo.trim(),
          base_branch: govBaseBranch.trim() || "main",
          base_commit: govBaseCommit.trim() || null,
          head_branch: govHeadBranch.trim(),
          governance_skills: null,
        };
        const r = await apiFetch(`${API}/sdlc/governance`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!r.ok) {
          const text = await r.text();
          let msg = text;
          try { msg = JSON.parse(text)?.detail || text; } catch { /* use raw */ }
          throw new Error(msg);
        }
        const d = await r.json();
        onTriggered(d.run_id);
        onClose();
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
      return;
    }

    const errors = {
      jiraKey: validateField("jiraKey", jiraKey),
      summary: validateField("summary", summary),
      repo:    validateField("repo", repo),
      branch:  validateField("branch", branch),
    };
    setFormErrors(errors);
    if (Object.values(errors).some(e => e)) return;

    if (!jiraKey.trim() || !summary.trim()) {
      setError("Jira key and summary are required.");
      return;
    }
    if (!repo.trim() && !langOverride) {
      setError("Either a repository (org/repo-name) or a language override is required.");
      return;
    }
    setLoading(true); setError("");
    try {
      const payload = {
        jira_key:          jiraKey.trim(),
        summary:           summary.trim(),
        repo:              repo.trim(),
        language_override: langOverride,
        skip_tests:        !runTests,
        skip_slt:          !runSlt,
        run_governance_review: runGovernanceReview,
      };
      if (selectedProductId) payload.product_id = selectedProductId;
      if (branch.trim()) payload.branch = branch.trim();
      if (multiRepoEnabled && deps.length > 0) {
        payload.dependencies = deps
            .filter(d => d.repo && d.repo.trim())
            .map(d => ({ repo: d.repo.trim(), ref: d.ref || undefined, kind: d.kind }));
      }

      const r = await apiFetch(`${API}/sdlc/${type}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const text = await r.text();
        let msg = text;
        try { msg = JSON.parse(text)?.detail || text; } catch { /* use raw */ }
        throw new Error(msg);
      }
      const d = await r.json();
      onTriggered(d.run_id);
      onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
      <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
        <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
          <h2 className="text-base font-semibold text-gray-800 mb-4">Trigger SDLC Pipeline</h2>

          <div className="flex gap-2 mb-4">
            {["feature", "bug", "governance"].map(t => (
                <button
                    key={t}
                    onClick={() => {
                      if (t === type) return;
                      setType(t);
                      // Reset all form fields so each tab starts fresh
                      setJiraKey(""); setSummary(""); setJiraDesc(""); setRepo(""); setBranch("");
                      setBranchOverridden(false); setLangOverride(""); setRunTests(false);
                      setRunSlt(false); setRunGovernanceReview(false);
                      setGovBaseBranch("main"); setGovBaseCommit(""); setGovHeadBranch("");
                      setSelectedProductId(""); setProductRepos([]); setDeps([]);
                      setJiraMsg(null); setFormErrors(EMPTY_ERRORS); setError("");
                    }}
                    className={`flex-1 py-1.5 text-sm rounded border font-medium transition-colors cursor-pointer
                ${type === t ? "bg-gradient-to-br from-indigo-600 to-violet-600 hover:opacity-70 text-white " : "text-gray-600 border-gray-200 hover:bg-gray-100"}`}
                >
                  {t === "feature" ? "Feature" : t === "bug" ? "Bug Fix" : "Governance"}
                </button>
            ))}
          </div>

          <div className="space-y-3">
            {type !== "governance" ? (<>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Jira Key *</label>
              <div className="relative">
                <input
                    className={`w-full border rounded px-3 py-2 text-sm focus:outline-none pr-8 ${formErrors.jiraKey ? "border-red-400 focus:border-red-400" : "border-gray-200 focus:border-indigo-300"}`}
                    placeholder="e.g. AiNxt-1234"
                    value={jiraKey}
                    onChange={e => { handleChange("jiraKey", e.target.value, setJiraKey); onJiraKeyChange(e.target.value); }}
                    onBlur={e => { handleBlur("jiraKey", e.target.value); fetchJiraDetails(e.target.value.trim().toUpperCase()); }}
                />
                {jiraLoading && (
                    <Loader2 size={13} className="animate-spin absolute right-2.5 top-2.5 text-indigo-400" />
                )}
              </div>
              {formErrors.jiraKey && (
                  <p className="text-xs mt-1 text-red-500">{formErrors.jiraKey}</p>
              )}
              {jiraMsg && (
                  <p className={`text-xs mt-1 ${jiraMsg.type === "ok" ? "text-green-600" : "text-amber-600"}`}>
                    {jiraMsg.text}
                  </p>
              )}
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Summary *</label>
              <input
                  className={`w-full border rounded px-3 py-2 text-sm focus:outline-none ${formErrors.summary ? "border-red-400 focus:border-red-400" : "border-gray-200 focus:border-indigo-300"}`}
                  placeholder="Short description of the issue"
                  value={summary}
                  onChange={e => handleChange("summary", e.target.value, setSummary)}
                  onBlur={e => handleBlur("summary", e.target.value)}
              />
              {formErrors.summary && (
                  <p className="text-xs mt-1 text-red-500">{formErrors.summary}</p>
              )}
              {jiraDesc && (
                  <p className="text-xs text-gray-500 mt-1 italic line-clamp-3">{jiraDesc}</p>
              )}
            </div>

            {/* Product → Repo → Branch cascade */}
            <div className="border border-gray-100 rounded-lg p-3 bg-gray-50 space-y-2">
              <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Branch Selection</p>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Product
                  <span className="ml-1 text-gray-400 font-normal">(optional — auto-fills repo + branch)</span>
                </label>
                <select
                    className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-300 bg-white"
                    value={selectedProductId}
                    onChange={e => onProductChange(e.target.value)}
                    disabled={productsLoading}
                >
                  <option value="">— Select product (optional) —</option>
                  {products.map(p => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
                {productsLoading && <p className="text-xs text-gray-400 mt-0.5">Loading products...</p>}
              </div>

              {selectedProductId && (
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">
                      Repository *
                    </label>
                    <select
                        className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-300 bg-white"
                        value={repo}
                        onChange={e => onProductRepoChange(e.target.value)}
                        disabled={reposLoading}
                    >
                      <option value="">— Select repository —</option>
                      {productRepos.map(r => (
                          <option key={r.repo} value={r.repo}>{r.repo} ({r.branch})</option>
                      ))}
                    </select>
                    {reposLoading && <p className="text-xs text-gray-400 mt-0.5">Loading repos...</p>}
                  </div>
              )}

              {!selectedProductId && (
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">
                      Repository
                      <span className="ml-1 text-gray-400 font-normal">(or select product above)</span>
                    </label>
                    <input
                        className={`w-full border bg-white rounded px-3 py-2 text-sm focus:outline-none ${formErrors.repo ? "border-red-400 focus:border-red-400" : "border-gray-200 focus:border-indigo-300"}`}
                        placeholder="org/repo-name"
                        value={repo}
                        onChange={e => handleChange("repo", e.target.value, setRepo)}
                        onBlur={e => handleBlur("repo", e.target.value)}
                    />
                    {formErrors.repo && (
                        <p className="text-xs mt-1 text-red-500">{formErrors.repo}</p>
                    )}
                  </div>
              )}

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Base Branch
                  <span className="ml-1 text-gray-400 font-normal">(auto-filled from product config)</span>
                </label>
                <input
                    className={`w-full border bg-white rounded px-3 py-2 text-sm focus:outline-none ${formErrors.branch ? "border-red-400 focus:border-red-400" : "border-gray-200 focus:border-indigo-300"}`}
                    placeholder="e.g. develop, release/2.0 (leave blank to auto-detect)"
                    value={branch}
                    onChange={e => { handleChange("branch", e.target.value, setBranch); setBranchOverridden(true); }}
                    onBlur={e => handleBlur("branch", e.target.value)}
                />
                {formErrors.branch && (
                    <p className="text-xs mt-1 text-red-500">{formErrors.branch}</p>
                )}
                {branch && !formErrors.branch && (
                    <p className="text-xs text-indigo-600 mt-0.5">
                      Working branch will be: <code className="font-mono">{type === "bug" ? "fix" : "feature"}/{(jiraKey || "JIRA-KEY").toLowerCase()}-...</code>
                    </p>
                )}
              </div>
            </div>

            {multiRepoEnabled && (
                <div className="border border-gray-100 rounded-lg p-3 bg-gray-50">
                  <DepTable
                      deps={deps}
                      onChange={setDeps}
                      primaryRepo={repo}
                      primaryBranch={branch}
                  />
                </div>
            )}

            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Language Override
                <span className="ml-1 text-gray-400 font-normal">(required if repo/GitLab not configured)</span>
              </label>
              <select
                  className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-300 bg-white"
                  value={langOverride}
                  onChange={e => setLangOverride(e.target.value)}
              >
                {LANGUAGE_OPTIONS.map(o => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              {!langOverride && !repo.trim() && (
                  <p className="text-xs text-amber-600 mt-1">
                    If auto-detect fails (GitLab unreachable / repo not indexed), pipeline will be blocked.
                    Select a language to guarantee it runs.
                  </p>
              )}
            </div>

            {/* W-B: Pipeline options — skip_tests + skip_slt */}
            <div className="border border-gray-100 rounded-lg p-3 bg-gray-50 space-y-2">
              <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Pipeline Options</p>
              <label className="flex items-center gap-2 cursor-pointer group">
                <input
                  type="checkbox"
                  checked={runTests}
                  onChange={e => setRunTests(e.target.checked)}
                  className="w-3.5 h-3.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-400"
                />
                <span className="text-xs text-gray-700 group-hover:text-gray-900">Run Tests + SLT</span>
                <span className="text-[10px] text-gray-400">(runs TESTING and SLT_RUNNING stages; uncheck to skip)</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer group" title="Generates system-level test files during CODING; independent of Run Tests">
                <input
                  type="checkbox"
                  checked={runSlt}
                  onChange={e => setRunSlt(e.target.checked)}
                  className="w-3.5 h-3.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-400"
                />
                <span className="text-xs text-gray-700 group-hover:text-gray-900">Run SLT Generation</span>
                <span className="text-[10px] text-gray-400">(generates system-level test files during coding)</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer group" title="Runs the EA/IS/DPDP governance skills over the diff after REVIEW">
                <input
                  type="checkbox"
                  checked={runGovernanceReview}
                  onChange={e => setRunGovernanceReview(e.target.checked)}
                  className="w-3.5 h-3.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-400"
                />
                <span className="text-xs text-gray-700 group-hover:text-gray-900">Run Governance Review</span>
                <span className="text-[10px] text-gray-400">(EA / IS / DPDP skills over the diff; suspends on unresolved findings)</span>
              </label>
            </div>
            </>) : (
              /* ── Governance-only form fields ── */
              <>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">
                    Product
                    <span className="ml-1 text-gray-400 font-normal">(optional — auto-fills repo)</span>
                  </label>
                  <select
                    className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-300 bg-white"
                    value={selectedProductId}
                    onChange={e => onProductChange(e.target.value)}
                    disabled={productsLoading}
                  >
                    <option value="">— Select product (optional) —</option>
                    {products.map(p => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                  {productsLoading && <p className="text-xs text-gray-400 mt-0.5">Loading products...</p>}
                </div>

                {selectedProductId ? (
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Repository *</label>
                    <select
                      className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-300 bg-white"
                      value={repo}
                      onChange={e => {
                        const repoName = e.target.value;
                        setRepo(repoName);
                        // Auto-fill base branch from product repo config
                        const found = productRepos.find(r => r.repo === repoName);
                        if (found && found.branch) setGovBaseBranch(found.branch);
                      }}
                      disabled={reposLoading}
                    >
                      <option value="">— Select repository —</option>
                      {productRepos.map(r => (
                        <option key={r.repo} value={r.repo}>{r.repo} ({r.branch})</option>
                      ))}
                    </select>
                    {reposLoading && <p className="text-xs text-gray-400 mt-0.5">Loading repos...</p>}
                  </div>
                ) : (
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">
                      Repository *
                      <span className="ml-1 text-gray-400 font-normal">(or select product above)</span>
                    </label>
                    <input
                      className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-300"
                      placeholder="org/repo-name"
                      value={repo}
                      onChange={e => setRepo(e.target.value)}
                    />
                  </div>
                )}

                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">
                    Base / production branch
                  </label>
                  <input
                    className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-300"
                    placeholder="main"
                    value={govBaseBranch}
                    onChange={e => setGovBaseBranch(e.target.value)}
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">
                    Production base commit SHA
                    <span className="ml-1 text-gray-400 font-normal">(optional)</span>
                  </label>
                  <input
                    className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-300"
                    placeholder="abc1234 (leave blank to use branch HEAD)"
                    value={govBaseCommit}
                    onChange={e => setGovBaseCommit(e.target.value)}
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Current / head branch to scan *</label>
                  <input
                    className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-300"
                    placeholder="feature/my-branch or fix/my-fix"
                    value={govHeadBranch}
                    onChange={e => setGovHeadBranch(e.target.value)}
                  />
                </div>
              </>
            )}

            {error && <p className="text-xs text-red-600">{error}</p>}
          </div>


          <div className="mt-5 flex gap-2 justify-end">
            <button onClick={onClose} className="px-3 py-1.5 text-sm text-gray-600 hover:text-gray-800 cursor-pointer">
              Cancel
            </button>
            <button
                onClick={submit}
                disabled={loading}
                className="flex items-center gap-1.5 px-4 py-1.5 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700 disabled:opacity-50 cursor-pointer"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              Start Pipeline
            </button>
          </div>
        </div>
      </div>
  );
}

// ── Outputs Tab ──────────────────────────────────────────────

function Section({ title, children }) {
  return (
      <div>
        <p className="font-semibold text-gray-700 mb-1.5 text-xs uppercase tracking-wide">{title}</p>
        <div className="bg-gray-50 rounded-lg p-3 space-y-1.5 border border-gray-100">{children}</div>
      </div>
  );
}

function Row({ label, value, mono, color }) {
  if (!value && value !== 0) return null;
  return (
      <p className="text-xs">
        <span className="text-gray-500 mr-1">{label}:</span>
        <span className={`${mono ? "font-mono text-indigo-700" : ""} ${color || ""}`}>{value}</span>
      </p>
  );
}

function FileList({ files, label }) {
  const [showAll, setShowAll] = useState(false);
  if (!Array.isArray(files) || files.length === 0) return null;
  const visible = showAll ? files : files.slice(0, 6);
  return (
      <div className="mt-1">
        {label && <p className="text-gray-500 text-xs mb-0.5">{label}</p>}
        <ul className="space-y-0.5">
          {visible.map((f, i) => (
              <li key={i} className="font-mono text-[11px] text-indigo-700 truncate">{typeof f === "string" ? f : f.path || f.file || JSON.stringify(f)}</li>
          ))}
          {files.length > 6 && (
              <li><button className="text-[11px] text-indigo-500 hover:text-indigo-700 cursor-pointer" onClick={() => setShowAll(v => !v)}>
                {showAll ? "Show less ↑" : `+${files.length - 6} more…`}
              </button></li>
          )}
        </ul>
      </div>
  );
}

function BulletList({ items, label }) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
      <div className="mt-1">
        {label && <p className="text-gray-500 text-xs mb-0.5">{label}</p>}
        <ul className="space-y-0.5 ml-2">
          {items.map((item, i) => (
              <li key={i} className="text-xs text-gray-700 list-disc ml-2">{typeof item === "string" ? item : JSON.stringify(item)}</li>
          ))}
        </ul>
      </div>
  );
}

function OutputsTab({ ctx, run }) {
  const isGov = run.type === "governance";
  // Governance runs carry no classification/triage/etc. context — surface their
  // own artifacts (scanned branch, governance-fix branch, commit, MR) instead of
  // early-returning "No outputs yet".
  const hasGov = isGov && (ctx.head_branch || ctx.governance_fix_branch ||
      run.pr_url || run.repo || ctx.repo || run.branch);
  const hasAny = ctx.repo_ctx || ctx.classification || ctx.triage || ctx.analysis ||
      ctx.design || ctx.fix || ctx.rca || ctx.code_output || ctx.test_result ||
      ctx.pr_review || ctx.jira_url || ctx.confluence_url || hasGov;

  if (!hasAny) {
    return <p className="text-xs text-gray-400 py-6 text-center">No outputs yet — pipeline is starting up.</p>;
  }

  // ── Governance outcome (standalone governance runs) ──
  const govFixBranch = ctx.governance_fix_branch || "";
  const govScanned   = ctx.head_branch || run.branch || "";


  const cls  = ctx.classification || {};
  const tri  = ctx.triage || {};
  const ana  = ctx.analysis || {};
  const des  = ctx.design || ctx.fix || {};
  const rca  = ctx.rca || {};
  const code = ctx.code_output || {};
  const test = ctx.test_result || {};
  const rev  = ctx.pr_review || {};
  const repo = ctx.repo_ctx || {};

  const isBug     = !!ctx.triage;
  const isFeature = !!ctx.classification;

  // Severity colour
  const sevColor = tri.severity === "Critical" ? "text-red-600 font-semibold"
      : tri.severity === "High" ? "text-orange-600 font-semibold"
          : tri.severity === "Medium" ? "text-amber-600"
              : "text-gray-700";

  // Code files
  const codeFiles = (code.files || []).filter(f => !f.is_test);
  const testFiles = (code.files || []).filter(f => f.is_test);

  // Test summary
  const testStatus = test._build_status || test.status || "";
  const testPassed = test.passed ?? test.tests_passed;
  const testFailed = test.failed ?? test.tests_failed;

  return (
      <div className="space-y-4 text-xs">

        {/* ── Governance Outcome (standalone governance runs) ── */}
        {isGov && (
            <Section title="Governance Outcome">
              <Row label="Repo" value={run.repo || ctx.repo} color="font-mono text-gray-700" />
              {govScanned && <Row label="Scanned Branch" value={govScanned} color="font-mono" />}
              {ctx.base_branch && <Row label="Base Branch" value={ctx.base_branch} color="font-mono" />}
              {govFixBranch && (
                  <p className="text-xs">
                    <span className="text-gray-500">Governance-fix Branch:</span>{" "}
                    <span className="font-mono text-violet-600">{govFixBranch}</span>
                  </p>
              )}
              {run.pr_url ? (
                  <p className="text-xs">
                    <a href={run.pr_url} target="_blank" rel="noopener noreferrer"
                       className="inline-flex items-center gap-1 text-teal-600 hover:underline font-medium">
                      <GitPullRequest size={11} /> Merge Request →
                    </a>
                  </p>
              ) : run.state === "COMPLETE" ? (
                  <p className="text-xs text-gray-500">
                    Completed with no merge request — no governance code fix was needed.
                  </p>
              ) : null}
            </Section>
        )}

        {/* ── Repo Detection ── */}
        {repo.language && (
            <Section title="Repo Detection">
              <Row label="Language" value={repo.language} color="font-medium text-indigo-700" />
              <Row label="Tech Stack" value={repo.tech_stack} />
              <Row label="Framework" value={repo.framework} />
              <Row label="Test Framework" value={repo.test_framework} />
              {repo.confidence != null && (
                  <p className="text-xs text-gray-500">
                    Confidence: <span className="font-medium">{(repo.confidence * 100).toFixed(0)}%</span>
                    <span className="text-gray-400 ml-1">({repo.detection_source || "unknown"})</span>
                  </p>
              )}
            </Section>
        )}

        {/* ── Classification (feature) ── */}
        {isFeature && cls.core_intent && (
            <Section title="Classification">
              <p className="text-xs text-gray-700"><span className="text-gray-500">Intent:</span> {cls.core_intent}</p>
              <div className="flex gap-4 flex-wrap">
                <Row label="Complexity" value={cls.complexity} color="font-medium" />
                <Row label="Effort" value={cls.effort_estimate} />
              </div>
              {cls.affected_components?.length > 0 && <FileList files={cls.affected_components} label="Affected Files" />}
              {cls.risks?.length > 0 && <BulletList items={cls.risks} label="Risks" />}
            </Section>
        )}

        {/* ── Triage (bug) ── */}
        {isBug && (tri.severity || tri.category) && (
            <Section title="Bug Triage">
              <div className="flex gap-4 flex-wrap">
                {tri.severity && <p className="text-xs"><span className="text-gray-500">Severity:</span> <span className={sevColor}>{tri.severity}</span></p>}
                <Row label="Category" value={tri.category} />
                <Row label="Assignee" value={tri.assignee_role} />
                <Row label="Reproduction" value={tri.reproduction} />
              </div>
              {tri.affected_components?.length > 0 && <FileList files={tri.affected_components} label="Affected Files" />}
              {tri.triage_steps?.length > 0 && <BulletList items={tri.triage_steps} label="Triage Steps" />}
            </Section>
        )}

        {/* ── Root Cause (bug) ── */}
        {rca.root_cause && (
            <Section title="Root Cause Analysis">
              <p className="text-xs text-gray-700 leading-relaxed">{rca.root_cause}</p>
              {rca.code_path && <p className="text-xs mt-1"><span className="text-gray-500">Code Path:</span> <span className="font-mono text-[11px]">{rca.code_path}</span></p>}
              {rca.missing_test && <p className="text-xs mt-1"><span className="text-gray-500">Missing Test:</span> {rca.missing_test}</p>}
            </Section>
        )}

        {/* ── Analysis (feature) ── */}
        {ana.sub_tasks?.length > 0 && (
            <Section title="Technical Analysis">
              <BulletList items={ana.sub_tasks} label="Sub-tasks" />
              {(ana.files_to_change?.length > 0 || ana.new_files_needed?.length > 0) && (
                  <FileList files={[...(ana.files_to_change || []), ...(ana.new_files_needed || [])]} label="Files to Change" />
              )}
              {ana.regression_risk && <Row label="Regression Risk" value={typeof ana.regression_risk === "object" ? JSON.stringify(ana.regression_risk) : ana.regression_risk} />}
            </Section>
        )}

        {/* ── Solution Design / Fix Design ── */}
        {(des.solution_approach || des.fix_description || des.fix_approach) && (
            <Section title={isBug ? "Fix Design" : "Solution Design"}>
              <p className="text-xs text-gray-700 leading-relaxed">
                {des.solution_approach || des.fix_description || des.fix_approach}
              </p>
              {des.implementation_plan?.length > 0 && <BulletList items={des.implementation_plan} label="Implementation Plan" />}
              {des.testing_strategy && typeof des.testing_strategy === "string" && (
                  <Row label="Testing" value={des.testing_strategy} />
              )}
              {des.rollback_strategy && typeof des.rollback_strategy === "string" && (
                  <Row label="Rollback" value={des.rollback_strategy} />
              )}
            </Section>
        )}

        {/* ── Generated Code ── */}
        {(codeFiles.length > 0 || testFiles.length > 0) && (
            <Section title={`Generated Code (${codeFiles.length} impl${testFiles.length > 0 ? ` · ${testFiles.length} test` : ""})`}>
              {codeFiles.length > 0 && <FileList files={codeFiles} label="Implementation Files" />}
              {testFiles.length > 0 && <FileList files={testFiles} label="Test Files" />}
            </Section>
        )}

        {/* ── Test Results ── */}
        {(testStatus || testPassed != null) && (
            <Section title="Test Results">
              {testStatus && (
                  <p className="text-xs">
                    <span className="text-gray-500">Status:</span>{" "}
                    <span className={`font-medium ${testStatus === "PASS" || testStatus === "TESTS_PASSED" ? "text-green-600" : testStatus === "FAIL" || testStatus === "TESTS_FAILED" ? "text-red-600" : "text-gray-700"}`}>
                {testStatus}
              </span>
                  </p>
              )}
              {testPassed != null && <Row label="Passed" value={testPassed} color="text-green-600" />}
              {testFailed != null && <Row label="Failed" value={testFailed} color={testFailed > 0 ? "text-red-600 font-semibold" : "text-gray-700"} />}
              {test.errors?.length > 0 && <BulletList items={test.errors.slice(0, 5)} label="Errors" />}
            </Section>
        )}

        {/* ── PR Review ── */}
        {(rev.score != null || rev.approved != null) && (
            <Section title="PR Review">
              <div className="flex gap-4 flex-wrap">
                {rev.score != null && (
                    <p className="text-xs">
                      <span className="text-gray-500">Score:</span>{" "}
                      <span className={`font-semibold ${rev.score >= 8 ? "text-green-600" : rev.score >= 6 ? "text-amber-600" : "text-red-600"}`}>
                  {rev.score}/10
                </span>
                    </p>
                )}
                {rev.approved != null && (
                    <p className="text-xs">
                      <span className="text-gray-500">Decision:</span>{" "}
                      <span className={`font-semibold ${rev.approved ? "text-green-600" : "text-red-600"}`}>
                  {rev.approved ? "Approved" : "Changes Requested"}
                </span>
                    </p>
                )}
              </div>
              {rev.summary && <p className="text-xs text-gray-700 mt-1 leading-relaxed">{rev.summary}</p>}
              {rev.blocking_issues?.length > 0 && <BulletList items={rev.blocking_issues} label="Blocking Issues" />}
              {rev.suggestions?.length > 0 && <BulletList items={rev.suggestions} label="Suggestions" />}
            </Section>
        )}

        {/* ── Links ── */}
        {(() => {
          const confUrl = run.confluence_url || ctx.confluence_url;
          return (ctx.jira_url || run.jira_key || confUrl || ctx.gitlab_issue_url || run.pr_url) && (
            <Section title="Links">
              {(ctx.jira_url || run.jira_key) && (
                  <p className="text-xs">
                    {ctx.jira_url
                        ? <a href={ctx.jira_url} target="_blank" rel="noopener noreferrer" className="text-orange-500 hover:underline">Jira: {run.jira_key} →</a>
                        : <span className="text-gray-700">Jira: {run.jira_key}</span>
                    }
                  </p>
              )}
              {confUrl && <p className="text-xs"><a href={confUrl} target="_blank" rel="noopener noreferrer" className="text-blue-500 hover:underline">Confluence Design Doc →</a></p>}
              {ctx.gitlab_issue_url && <p className="text-xs"><a href={ctx.gitlab_issue_url} target="_blank" rel="noopener noreferrer" className="text-indigo-500 hover:underline">GitLab Issue →</a></p>}
              {run.pr_url && <p className="text-xs"><a href={run.pr_url} target="_blank" rel="noopener noreferrer" className="text-teal-600 hover:underline">Pull Request →</a></p>}
              {run.branch && <p className="text-xs text-gray-500">Branch: <span className="font-mono">{run.branch}</span></p>}
            </Section>
          );
        })()}

      </div>
  );
}

// ── Context Tab (raw debug view, skip noisy/empty keys) ───────

const _CTX_SKIP = new Set([
  "repo_ctx", "classification", "triage", "analysis", "design", "fix", "rca",
  "code_output", "test_result", "pr_review", "jira_url", "confluence_url",
  "gitlab_issue_url", "hitl_deadline", "base_branch", "working_branch",
]);

function ContextTab({ ctx }) {
  const entries = Object.entries(ctx).filter(([k, v]) => {
    if (_CTX_SKIP.has(k)) return false;
    if (v == null || v === "" || v === false) return false;
    if (typeof v === "object" && Object.keys(v).length === 0) return false;
    if (Array.isArray(v) && v.length === 0) return false;
    return true;
  });

  if (entries.length === 0) {
    return <p className="text-xs text-gray-400 py-4 text-center">No additional context data.</p>;
  }

  return (
      <div className="space-y-2">
        {entries.map(([k, v]) => (
            <div key={k}>
              <p className="text-xs font-medium text-gray-600 mb-0.5 uppercase tracking-wide">{k}</p>
              <pre className="text-xs bg-gray-50 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-40 border border-gray-100">
            {typeof v === "object" ? JSON.stringify(v, null, 2) : String(v)}
          </pre>
            </div>
        ))}
      </div>
  );
}

// ── Copy helper: extract plain-text for each tab ────────────

function _extractTabText(tab, { events, ctx, run }) {
  if (tab === "timeline") {
    if (!events?.length) return "";
    return events.map(ev => {
      const ts = ev.created_at || "";
      const stage = ev.stage || ev.to_state || "";
      const actor = ev.actor || "system";
      const body = ev.data?.structured || ev.output
          || (ev.data && Object.keys(ev.data).length > 0 ? JSON.stringify(ev.data, null, 2) : "");
      return `[${ts}] ${stage} (${actor})\n${body}`;
    }).join("\n\n");
  }
  if (tab === "outputs") {
    const sections = [];
    const _j = (obj) => typeof obj === "object" ? JSON.stringify(obj, null, 2) : String(obj || "");
    if (ctx.repo_ctx)       sections.push(`── Repo Detection ──\n${_j(ctx.repo_ctx)}`);
    if (ctx.classification) sections.push(`── Classification ──\n${_j(ctx.classification)}`);
    if (ctx.triage)         sections.push(`── Bug Triage ──\n${_j(ctx.triage)}`);
    if (ctx.rca)            sections.push(`── Root Cause Analysis ──\n${_j(ctx.rca)}`);
    if (ctx.analysis)       sections.push(`── Technical Analysis ──\n${_j(ctx.analysis)}`);
    if (ctx.design || ctx.fix) sections.push(`── ${ctx.triage ? "Fix" : "Solution"} Design ──\n${_j(ctx.design || ctx.fix)}`);
    if (ctx.code_output)    sections.push(`── Generated Code ──\n${_j(ctx.code_output)}`);
    if (ctx.test_result)    sections.push(`── Test Results ──\n${_j(ctx.test_result)}`);
    if (ctx.pr_review)      sections.push(`── PR Review ──\n${_j(ctx.pr_review)}`);
    if (ctx.jira_url)       sections.push(`Jira: ${ctx.jira_url}`);
    if (ctx.confluence_url) sections.push(`Confluence: ${ctx.confluence_url}`);
    return sections.join("\n\n") || "";
  }
  if (tab === "context") {
    const skip = _CTX_SKIP;
    const entries = Object.entries(ctx || {}).filter(([k, v]) => {
      if (skip.has(k)) return false;
      if (v == null || v === "" || v === false) return false;
      if (typeof v === "object" && Object.keys(v).length === 0) return false;
      if (Array.isArray(v) && v.length === 0) return false;
      return true;
    });
    return entries.map(([k, v]) => `${k}:\n${typeof v === "object" ? JSON.stringify(v, null, 2) : String(v)}`).join("\n\n");
  }
  if (tab === "error") {
    return run?.error || "";
  }
  return "";
}

// ── Run Detail Panel ─────────────────────────────────────────

function RunDetail({ run, events, onApprovalDone, onClose, onRetrigger, user }) {
  const [activeTab, setActiveTab] = useState(run?.state === "FAILED" ? "error" : "timeline");
  const [verifyResult, setVerifyResult] = useState(null);
  const [copiedTab, setCopiedTab] = useState(null);
  const [artifactStage, setArtifactStage] = useState(null); // {stage, label} | null
  // The stage-action panel is a SUSPENDED-only affordance. Drive its visibility off
  // the LIVE run.state — never off run.context.suspended_at_stage, which the backend
  // does not clear on resume. Keying off the stale context value made the panel linger
  // after the run moved on, and re-appear on every re-mount. `panelDismissed` lets the
  // user close it while still suspended; it resets when the run (re-)enters SUSPENDED.
  const [panelDismissed, setPanelDismissed] = useState(false);
  const [govReviewLoading, setGovReviewLoading] = useState(false);
  const [copiedRunId, setCopiedRunId] = useState(false);
  const prevRunStateRef = useRef(run?.state);
  const { toast } = useToast();

  const ctx = run.context || {};

  // Explore/planning stages that get the navigator feed.
  const EXPLORE_STATES = ["PLAN", "ANALYZING", "DESIGNING", "DIAGNOSING"];
  const isExploring = EXPLORE_STATES.includes(run.state);
  // Layered status: AWAITING_*/COMMIT_FAILED/SUSPENDED need human attention.
  const attention = needsAttention(run.state);

  const copyTabContent = (tab) => {
    const text = _extractTabText(tab, { events, ctx, run });
    if (!text) { toast.info("Nothing to copy"); return; }
    navigator.clipboard.writeText(text).then(() => {
      setCopiedTab(tab);
      setTimeout(() => setCopiedTab(null), 1500);
    }).catch(() => toast.error("Copy failed"));
  };

  // Auto-switch to error tab ONLY when run first transitions into FAILED
  // (not on every render) — so the user can still switch tabs while FAILED.
  useEffect(() => {
    const prev = prevRunStateRef.current;
    if (run?.state === "FAILED" && prev !== "FAILED") setActiveTab("error");
    prevRunStateRef.current = run?.state;
  }, [run?.state]);

  // Re-arm the suspended action panel whenever the run (re-)enters SUSPENDED, so a
  // prior dismissal doesn't suppress the panel for a brand-new suspension.
  useEffect(() => {
    if (run?.state === "SUSPENDED") setPanelDismissed(false);
  }, [run?.state]);

  const exportReport = async () => {
    try {
      const r = await apiFetch(`${API}/compliance/runs/${run.id}/report`);
      if (!r.ok) throw new Error(`Export failed: ${r.status} ${r.statusText}`);
      const data = await r.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `compliance-report-${run.id.slice(0, 8)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(`Export failed: ${e.message}`);
    }
  };

  const verifyChain = async () => {
    try {
      const r = await apiFetch(`${API}/compliance/runs/${run.id}/verify`);
      if (!r.ok) throw new Error(`Verify failed: ${r.status} ${r.statusText}`);
      const data = await r.json();
      setVerifyResult(data);
    } catch (e) {
      setVerifyResult({ valid: false, error: e.message });
    }
  };

  // Standalone governance-review trigger for an EXISTING run (independent of the
  // in-pipeline GOVERNANCE_REVIEW gate) — POST /sdlc/governance-review.
  // See agents/sdlc_governance/ (Step 9: run_id mode reuses the VERIFIED_DIFF diff).
  const runGovernanceNow = async () => {
    setGovReviewLoading(true);
    try {
      const r = await apiFetch(`${API}/sdlc/governance-review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: run.id, auto_fix: true, governance_skills: null }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        toast.error(err.detail || `Governance review request failed (${r.status})`);
        return;
      }
      const data = await r.json();
      toast.success(`Governance review enqueued${data?.job_id ? ` — job ${data.job_id}` : ""}`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setGovReviewLoading(false);
    }
  };

  return (
      <div className="h-full overflow-y-auto">

        {/* ── Header ── */}
        <div className="border-b border-gray-200 bg-white">
          {/* Row 1: ID + badge + action buttons */}
          <div className="px-4 py-2 flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              {run.type === "bug"
                  ? <Bug size={13} className="text-red-500 flex-shrink-0" />
                  : run.type === "governance"
                      ? <Shield size={13} className="text-violet-500 flex-shrink-0" />
                      : <GitBranch size={13} className="text-indigo-500 flex-shrink-0" />
              }
              <button
                title={`Copy full run ID: ${run.id}`}
                onClick={() => {
                  navigator.clipboard.writeText(run.id).then(() => {
                    setCopiedRunId(true);
                    setTimeout(() => setCopiedRunId(false), 1500);
                  }).catch(() => {});
                }}
                className="flex items-center gap-1 group cursor-pointer min-w-0"
              >
                <span className="font-semibold text-xs text-gray-800 truncate">
                  {run.type === 'governance'
                    ? `gov-${run.id.slice(0, 8)}`
                    : (run.jira_key || run.id.slice(0, 8))}
                </span>
                {copiedRunId
                  ? <Check size={10} className="text-green-500 flex-shrink-0" />
                  : <Copy size={10} className="text-gray-300 group-hover:text-gray-500 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" />
                }
              </button>
              {badge(run.state)}
              {verifyResult && (
                <span
                  title={verifyResult.valid ? `All ${verifyResult.verified} events verified` : `Verification failed at event ${verifyResult.first_invalid_index}`}
                  className={`text-[10px] px-1.5 py-0.5 rounded cursor-default flex-shrink-0 ${verifyResult.valid ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}
                >
                  {verifyResult.valid ? "Audit OK" : "Audit Fail"}
                </span>
              )}
            </div>
            <div className="flex items-center gap-1 flex-shrink-0">
              {_TERMINAL_STATES.has(run.state) && onRetrigger && (
                <button
                  onClick={() => onRetrigger({
                    type: run.type || "feature",
                    jira_key: run.jira_key || "",
                    summary: run.jira_summary || "",
                    repo: run.context?.repo || run.repo || "",
                    branch: run.context?.base_branch || "",
                    product_id: run.context?.product_id || undefined,
                    dependencies: run.context?.dependencies || undefined,
                    ...(run.type === "governance" ? {
                      base_branch: run.context?.base_branch || undefined,
                      base_commit: run.context?.base_commit || undefined,
                      head_branch: run.context?.head_branch || undefined,
                    } : {}),
                  })}
                  title="Start a new pipeline run with the same parameters"
                  className="flex items-center gap-1 text-[10px] text-green-600 hover:bg-green-50 rounded px-2 py-1 cursor-pointer transition"
                >
                  <RotateCcw size={10} /> Re-trigger
                </button>
              )}
              <button onClick={runGovernanceNow} disabled={govReviewLoading}
                title="Run the EA/IS/DPDP governance skills over this run's diff now"
                className="flex items-center gap-1 text-[10px] text-violet-600 hover:bg-violet-50 rounded px-2 py-1 cursor-pointer transition disabled:opacity-50">
                {govReviewLoading ? <Loader2 size={10} className="animate-spin" /> : <Shield size={10} />}
                Governance
              </button>
              <button onClick={verifyChain} title="Verify audit chain integrity"
                className="text-[10px] rounded px-2 py-1 text-indigo-600 hover:bg-indigo-50 cursor-pointer transition">
                Verify
              </button>
              <button onClick={exportReport} title="Export compliance report as JSON"
                className="text-[10px] text-gray-500 hover:bg-gray-100 rounded px-2 py-1 cursor-pointer transition">
                Export
              </button>
              <button onClick={onClose} title="Close"
                className="text-gray-400 hover:text-gray-600 hover:bg-gray-100 p-1 rounded cursor-pointer transition">
                <XIcon size={13} />
              </button>
            </div>
          </div>
          {/* Row 2: summary on its own line, links on the line below */}
          {(() => {
            const confluenceUrl = run.confluence_url || run.context?.confluence_url;
            const jiraUrl = run.context?.jira_url || run.jira_url;
            return (run.jira_summary || run.branch || run.pr_url || confluenceUrl) && (
              <div className="px-5 py-2 border-b border-gray-100 bg-gray-50/50">
                {run.jira_summary && (
                  <p className="text-xs text-gray-600 truncate mb-1">{run.jira_summary}</p>
                )}
                <div className="flex gap-3 text-[11px] flex-wrap items-center">
                  {run.branch && (
                    <span className="text-gray-400 flex items-center gap-1">
                      <GitBranch size={10} />
                      <code className="font-mono text-gray-500">{run.branch}</code>
                    </span>
                  )}
                  {run.type === "governance" && run.context?.head_branch && (
                    <span className="flex items-center gap-1 text-violet-600 font-mono">
                      <Shield size={9} /> {run.context.head_branch}
                    </span>
                  )}
                  {run.type !== "governance" && (jiraUrl || run.jira_key) && (
                    <a href={jiraUrl || `#`} target="_blank" rel="noopener noreferrer"
                       className="flex items-center gap-0.5 text-orange-500 hover:underline font-medium">
                      Jira: {run.jira_key} <ExternalLink size={9} />
                    </a>
                  )}
                  {run.pr_url && (
                    <a href={run.pr_url} target="_blank" rel="noopener noreferrer"
                       className="flex items-center gap-0.5 text-indigo-500 hover:underline font-medium">
                      <GitPullRequest size={10} /> MR <ExternalLink size={9} />
                    </a>
                  )}
                  {confluenceUrl && (
                    <a href={confluenceUrl} target="_blank" rel="noopener noreferrer"
                       className="flex items-center gap-0.5 text-blue-500 hover:underline font-medium">
                      <BookOpen size={10} /> Docs <ExternalLink size={9} />
                    </a>
                  )}
                </div>
              </div>
            );
          })()}
        </div>

        {/* ── Stage timeline ── */}
        <div className="px-4 py-2 border-b border-gray-100 overflow-x-auto">
          <PipelineStepper
            run={run}
            events={events}
            onNodeClick={(node) => setArtifactStage({ stage: node.id, label: node.label })}
          />
        </div>

        {/* ── Body ── */}
        <div className={`${attention ? "ring-1 ring-inset ring-amber-200" : ""}`}>

          {/* Layered status: navigator feed while exploring. */}
          {isExploring && (
            <div className="px-5 py-2 space-y-2">
              <NavigatorActivity run={run} />
            </div>
          )}

          {/* HITL panel */}
          {(run.state === "AWAITING_CODE_APPROVAL"
              || run.state === "AWAITING_DESIGN_APPROVAL"
              || run.state === "AWAITING_SOLUTION_APPROVAL"
              || run.state === "AWAITING_PR_APPROVAL"
              || run.state === "AWAITING_RE_REVIEW"
              || run.state === "MERGE_CONFLICT"
              || run.state === "AWAITING_BUILD_METADATA_APPROVAL"
              || run.state === "AWAITING_USER_INPUT") && (
              <div className="px-5 py-2">
                <ApprovalPanel run={run} onActionDone={onApprovalDone} user={user} />
              </div>
          )}

          {/* Governance domain-approval gate — auto-surfaced when run is waiting for domain sign-off */}
          {run.state === "AWAITING_GOVERNANCE_APPROVAL" && (
            <div className="px-5 py-2">
              <GovernanceReviewPanel
                runId={run.id}
                run={run}
                repo={run?.repo}
                productId={run?.context?.product_id}
                onRefresh={onApprovalDone}
              />
            </div>
          )}

          {/* Clarifying Q&A history — read-only, shown once the gate was answered */}
          {run.context?.user_answers?.length > 0 && run.state !== "AWAITING_USER_INPUT" && (
            <div className="px-5 py-2">
              <AnsweredQuestionsView answers={run.context.user_answers} />
            </div>
          )}

          {/* W-A: Retry-commit panel — shown only for COMMIT_FAILED (resumable, not terminal) */}
          {run.state === "COMMIT_FAILED" && (
            <div className="px-5 py-2">
              <RetryCommitButton run={run} onRetried={onApprovalDone} />
            </div>
          )}

          {/* Tabs */}
          <div className="flex items-center border-b border-gray-200 px-5 pt-2 bg-white">
            {["timeline", "outputs", "context", "error"].map(tab => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`mr-4 pb-1.5 text-xs font-medium capitalize border-b-2 transition-colors cursor-pointer
                  ${activeTab === tab ? "border-indigo-600 text-indigo-600" : "border-transparent text-gray-400 hover:text-gray-600"}`}
              >
                {tab}
                {tab === "error" && run?.state === "FAILED" && run?.error && (
                    <span className="w-1.5 h-1.5 rounded-full bg-red-500 inline-block" />
                )}
              </button>
            ))}
            <button
              onClick={() => copyTabContent(activeTab)}
              title={`Copy ${activeTab} content`}
              className="ml-auto mb-1 p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors cursor-pointer"
            >
              {copiedTab === activeTab
                ? <Check size={13} className="text-green-500" />
                : <Copy size={13} />}
            </button>
          </div>

          {/* Tab content */}
          <div className="px-5 py-3">
            {activeTab === "timeline" && <EventLog events={events} />}

            {activeTab === "outputs" && <OutputsTab ctx={ctx} run={run} />}

            {activeTab === "context" && <ContextTab ctx={ctx} />}

            {activeTab === "error" && (
                run.error
                    ? <pre className="text-xs text-red-600 bg-red-50 rounded p-3 whitespace-pre-wrap">{run.error}</pre>
                    : <p className="text-xs text-gray-400 py-4 text-center">No errors.</p>
            )}
          </div>

          {/* Stage Action Panel — shown only while the run is live-SUSPENDED.
              Disappears the moment the run transitions to any other state. */}
          {run.state === "SUSPENDED" && !panelDismissed && run.context?.suspended_at_stage && (
            <div className="mt-4 px-4 space-y-3">
              {/* Compact manifest-validation verdict — PLAN suspends with
                  suspended_at_stage="PLAN" + suspend_reason="manifest validation
                  failed: ..." (agents/sdlc_pipeline.py _suspend_plan). Self-fetches
                  the MANIFEST_VALIDATION artifact; renders nothing if unavailable. */}
              {run.context.suspended_at_stage === "PLAN"
                && /manifest validation/i.test(run.context?.suspend_reason || "") && (
                <ManifestValidationBanner runId={run.id} />
              )}
              {run.context.suspended_at_stage === "BASELINE_BUILD" ? (
                /* BASELINE_BUILD runs before any artifact-backed stage, so the
                   generic retry/go_back/waive panel would 400 here. Use the
                   dedicated two-action baseline panel (re-enters the pipeline). */
                <BaselineActionPanel
                  runId={run.id}
                  run={run}
                  suspendReason={run.context?.suspend_reason}
                  onClose={() => setPanelDismissed(true)}
                  onDone={() => setPanelDismissed(true)}
                />
              ) : run.context.suspended_at_stage === "GOVERNANCE_SCAN" ? (
                /* GOVERNANCE_SCAN suspension (governance end-gate, 2026-07-24) —
                   the primary action here must be a governance resume
                   (target_stage="GOVERNANCE_SCAN"), never the generic
                   implement/plan retry, so this gets its own dedicated panel
                   instead of the generic StageActionPanel below. */
                <GovernanceResumePanel
                  runId={run.id}
                  onClose={() => setPanelDismissed(true)}
                  onDone={() => setPanelDismissed(true)}
                />
              ) : (
                <StageActionPanel
                  runId={run.id}
                  stage={run.context.suspended_at_stage}
                  runState={run.state}
                  runType={run.type}
                  onClose={() => setPanelDismissed(true)}
                  onDone={() => setPanelDismissed(true)}
                />
              )}
            </div>
          )}

        </div>

        {/* Stage Artifact Drawer */}
        {artifactStage && (
          <StageArtifactDrawer
            runId={run.id}
            stage={artifactStage.stage}
            stageLabel={artifactStage.label}
            run={run}
            events={events}
            onClose={() => setArtifactStage(null)}
          />
        )}
      </div>
  );
}

// ── Main Component ────────────────────────────────────────────

export default function SDLCPipeline({ user }) {
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const [events, setEvents] = useState([]);
  const [filter, setFilter] = useState("all");    // all | feature | bug | pr_review
  const [stateFilter, setStateFilter] = useState("all");
  const [searchQ, setSearchQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [showTrigger, setShowTrigger] = useState(false);
  const [retriggerDefaults, setRetriggerDefaults] = useState(null); // pre-fill for re-trigger
  const [stats, setStats] = useState(null);

  // ── Level guards (AD-level: 0=exec, 6=junior) ─────────────
  const _adLevel    = user?.ad_level ?? 6;
  const _isAdminSDLC = user?.role === "admin";
  const _canTrigger = _adLevel <= 6 || _isAdminSDLC;  // All levels can trigger SDLC runs
  const pollRef = useRef(null);
  // Ref mirrors selected state so polling intervals always see the current value
  // (avoids the classic stale-closure bug where setInterval captures an old reference)
  const selectedRef = useRef(null);

  // Load runs
  async function loadRuns() {
    try {
      const params = new URLSearchParams({ limit: 100 });
      if (filter !== "all") params.set("run_type", filter);
      const r = await apiFetch(`${API}/sdlc/runs?${params}`);
      const d = await r.json();
      const allRuns = d.runs || [];
      // NOTE: AWAITING_CODE_APPROVAL is the renamed AWAITING_DESIGN_APPROVAL (legacy rows
      // may still carry the old value) — the "Needs Approval" filter must match either.
      const _codeApprovalAliases = ["AWAITING_CODE_APPROVAL", "AWAITING_DESIGN_APPROVAL"];
      const filtered = stateFilter === "all"
          ? allRuns
          : _codeApprovalAliases.includes(stateFilter)
              ? allRuns.filter(r => _codeApprovalAliases.includes(r.state))
              : allRuns.filter(r => r.state === stateFilter);
      setRuns(filtered);
    } catch { /* ignore */ }
  }

  async function loadStats() {
    try {
      const r = await apiFetch(`${API}/sdlc/stats`);
      const d = await r.json();
      setStats(d);
    } catch { /* ignore */ }
  }

  async function loadEvents(runId) {
    try {
      const r = await apiFetch(`${API}/sdlc/runs/${runId}/events`);
      const d = await r.json();
      setEvents(d.events || []);
    } catch { /* ignore */ }
  }

  // Keep selectedRef in sync so the polling interval always sees the latest selection
  useEffect(() => { selectedRef.current = selected; }, [selected]);

  // Auto-poll — uses selectedRef to avoid stale closure
  useEffect(() => {
    loadRuns(); loadStats();
    pollRef.current = setInterval(async () => {
      loadRuns(); loadStats();
      const sel = selectedRef.current;
      if (sel) {
        loadEvents(sel.id);
        // Also refresh the selected run itself so its state stays current
        // (e.g. after approval, run transitions to COMPLETE and the panel auto-hides)
        try {
          const r = await apiFetch(`${API}/sdlc/runs/${sel.id}`);
          const d = await r.json();
          if (d.run) setSelected(d.run);
        } catch { /* ignore network errors */ }
      }
    }, 5000);
    return () => clearInterval(pollRef.current);
  }, [filter, stateFilter]);

  // When selection changes: load fresh run + events
  async function selectRun(run) {
    setLoading(true);
    try {
      const r = await apiFetch(`${API}/sdlc/runs/${run.id}`);
      const d = await r.json();
      setSelected(d.run || run);
      setEvents(d.events || []);
    } finally {
      setLoading(false);
    }
  }

  async function refreshSelected() {
    if (!selected) return;
    await selectRun(selected);
    await loadRuns();
  }

  function handleApprovalDone() {
    refreshSelected();
  }

  function handleTriggered(runId) {
    loadRuns();
    // Poll the new run for 15 seconds — if it immediately FAILED (e.g. language detection blocked),
    // auto-select it so the user sees the error in the detail panel without manual navigation.
    let attempts = 0;
    const earlyPoll = setInterval(async () => {
      attempts++;
      if (attempts > 6) { clearInterval(earlyPoll); return; } // stop after ~15s
      try {
        const r = await apiFetch(`${API}/sdlc/runs/${runId}`);
        const d = await r.json();
        const freshRun = d.run;
        if (!freshRun) { clearInterval(earlyPoll); return; }
        if (freshRun.state === "FAILED") {
          clearInterval(earlyPoll);
          setSelected(freshRun);
          setEvents(d.events || []);
          loadRuns();
        } else if (freshRun.state !== "CREATED") {
          // Pipeline is actively running — no need to keep polling for early failure
          clearInterval(earlyPoll);
          setSelected(freshRun);
          setEvents(d.events || []);
          loadRuns();
        }
      } catch { clearInterval(earlyPoll); }
    }, 2500);
  }

  // Stats bar
  const pendingApprovals = runs.filter(r =>
      r.state === "AWAITING_CODE_APPROVAL"
      || r.state === "AWAITING_DESIGN_APPROVAL"
      || r.state === "AWAITING_SOLUTION_APPROVAL"
      || r.state === "AWAITING_PR_APPROVAL"
      || r.state === "MERGE_CONFLICT"
      || r.state === "AWAITING_USER_INPUT"
      || r.state === "AWAITING_GOVERNANCE_APPROVAL"
  ).length;

  const inProgress = runs.filter(r => !_TERMINAL_STATES.has(r.state)
      && !["CREATED",
        "AWAITING_CODE_APPROVAL", "AWAITING_DESIGN_APPROVAL", "AWAITING_SOLUTION_APPROVAL",
        "AWAITING_PR_APPROVAL", "AWAITING_RE_REVIEW",
        "AWAITING_GOVERNANCE_APPROVAL", "AWAITING_USER_INPUT",
        "SUSPENDED", "COMMIT_FAILED", "MERGE_CONFLICT",
      ].includes(r.state)
  ).length;


  return (
      <div className="flex flex-col h-full bg-white">

        {/* Top bar */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-100">
          <div className="flex items-center gap-2">
            <GitBranch size={18} className="text-indigo-500" />
            <h1 className="text-sm font-semibold  text-indigo-700">SDLC Pipeline</h1>
            <span className="text-xs text-gray-400">AI-driven engineering lifecycle</span>
          </div>
          <div className="flex items-center gap-2">
            <button
                onClick={() => { loadRuns(); loadStats(); }}
                className="p-1.5 text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100"
            >
              <RefreshCw size={14} />
            </button>
            {_canTrigger && (
                <button
                    onClick={() => setShowTrigger(true)}
                    className="flex items-center gap-1.5 px-3 py-1.5 brand-grad hover:opacity-70 text-white text-xs rounded cursor-pointer"
                >
                  <PlusCircle size={13} /> New Pipeline
                </button>
            )}

          </div>
        </div>

        {/* Stats bar */}
        {stats && (
            <div className="flex items-center gap-4 px-5 py-2 bg-gray-50 border-b border-gray-100 text-xs text-gray-500 flex-wrap">
              <span>Total: <strong>{stats.total}</strong></span>
              {pendingApprovals > 0 && (
                  <span className="flex items-center gap-1 text-yellow-700 font-medium">
                    <AlertTriangle size={11} /> {pendingApprovals} awaiting approval
                  </span>
              )}
              {inProgress > 0 && (
                  <span className="flex items-center gap-1 text-blue-600">
                    <Loader2 size={11} className="animate-spin" /> {inProgress} running
                  </span>
              )}
              <span className="text-green-600">{stats.by_state?.COMPLETE || 0} complete</span>
              <span className="text-red-500">{stats.by_state?.FAILED || 0} failed</span>
            </div>
        )}

        {/* Filters */}
        <div className="flex items-center gap-2 px-4 py-2 border-b border-gray-100 flex-wrap">
          {[["all","All"], ["feature","Features"], ["bug","Bugs"], ["pr_review","PR Reviews"], ["governance","Governance"]].map(([v,l]) => (
              <button
                  key={v}
                  onClick={() => setFilter(v)}
                  className={`px-3 py-1 text-xs rounded-full transition-colors cursor-pointer
              ${filter === v ? "bg-gradient-to-br from-indigo-600 to-violet-600 hover:opacity-70 text-white" : "text-gray-600 outline-none border border-gray-100 hover:bg-gray-100"}`}
              >
                {l}
              </button>
          ))}
          <span className="text-gray-200 mx-1">|</span>
          {[
            ["all","All States"],
            ["AWAITING_DESIGN_APPROVAL","Needs Approval"],
            ["CODING","Running"],
            ["COMPLETE","Complete"],
            ["FAILED","Failed"],
          ].map(([v,l]) => {
            const isNeedsApproval = v === "AWAITING_DESIGN_APPROVAL";
            const showDot = isNeedsApproval && pendingApprovals > 0 && stateFilter !== v;
            return (
              <button
                  key={v}
                  onClick={() => setStateFilter(v)}
                  className={`relative px-3 py-1 text-xs rounded-full transition-colors cursor-pointer
              ${stateFilter === v ? "bg-gradient-to-br from-indigo-600 to-violet-600 hover:opacity-70 text-white" : "text-gray-600 outline-none border border-gray-100 hover:bg-gray-100"}`}
              >
                {l}
                {showDot && (
                  <span className="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full bg-amber-400 border border-white" />
                )}
              </button>
            );
          })}
        </div>

        {/* Main layout: list + detail */}
        <div className="flex flex-1 min-h-0">

          {/* Run list */}
          <div className="w-72 bg-gray-50 border-r border-gray-200 overflow-y-auto flex-shrink-0">
            {/* Search with clear button (ENH-2) */}
            <div className="px-3 py-2 border-b border-gray-100">
              <div className="relative">
                <input
                    value={searchQ}
                    onChange={e => setSearchQ(e.target.value)}
                    placeholder="Search runs..."
                    className="w-full px-2.5 py-1.5 text-xs border border-gray-200 rounded-md outline-none focus:border-indigo-300 bg-white shadow-sm pr-7"
                />
                {searchQ && (
                  <button
                    onClick={() => setSearchQ("")}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 cursor-pointer"
                    title="Clear search"
                  >
                    <XIcon size={12} />
                  </button>
                )}
              </div>
            </div>
            {(() => {
              const q = searchQ.toLowerCase();
              const filtered = runs.filter(r => {
                if (!searchQ) return true;
                if ((r.jira_key || "").toLowerCase().includes(q)) return true;
                if ((r.jira_summary || "").toLowerCase().includes(q)) return true;
                if (r.type === "governance" && r.id.toLowerCase().includes(q)) return true;
                if (r.type === "governance" && (r.context?.head_branch || "").toLowerCase().includes(q)) return true;
                if (r.type === "governance" && (r.context?.repo || r.repo || "").toLowerCase().includes(q)) return true;
                return false;
              });

              if (filtered.length === 0) {
                const hasFilters = filter !== "all" || stateFilter !== "all" || searchQ;
                return (
                  <div className="flex flex-col items-center justify-center h-full text-center px-6 py-12">
                    <GitBranch size={32} className="text-gray-300 mb-3" />
                    {hasFilters ? (
                      <>
                        <p className="text-sm text-gray-400 mb-1">No runs match your filters</p>
                        <button
                          onClick={() => { setFilter("all"); setStateFilter("all"); setSearchQ(""); }}
                          className="text-xs text-indigo-500 hover:text-indigo-700 mt-2 cursor-pointer underline"
                        >
                          Clear filters
                        </button>
                      </>
                    ) : (
                      <>
                        <p className="text-sm text-gray-400 mb-1">No pipeline runs yet</p>
                        <p className="text-xs text-gray-300">Click "New Pipeline" to start</p>
                      </>
                    )}
                  </div>
                );
              }

              return filtered.map(r => (
                <RunCard
                    key={r.id}
                    run={r}
                    onSelect={selectRun}
                    selected={selected?.id === r.id}
                    onCancelled={loadRuns}
                />
              ));
            })()}
          </div>

          {/* Detail panel */}
          <div className="flex-1 min-w-0 bg-white overflow-y-auto">
            {selected ? (
                <RunDetail
                    run={selected}
                    events={events}
                    onApprovalDone={handleApprovalDone}
                    onClose={() => setSelected(null)}
                    onRetrigger={(defs) => { setRetriggerDefaults(defs); setShowTrigger(true); }}
                    user={user}
                />
            ) : (
                <div className="flex flex-col items-center justify-center h-full text-center px-8">
                  <GitBranch size={40} className="text-gray-200 mb-4" />
                  <p className="text-sm text-gray-400 mb-1">Select a pipeline run to view details</p>
                  <p className="text-xs text-gray-300">
                    Active runs auto-refresh every 5 seconds
                  </p>
                </div>
            )}
          </div>

        </div>

        {/* Trigger modal */}
        {showTrigger && (
            <TriggerModal
                onClose={() => { setShowTrigger(false); setRetriggerDefaults(null); }}
                onTriggered={handleTriggered}
                defaults={retriggerDefaults}
            />
        )}

      </div>
  );
}

// ── Stage Artifact Renderers ──────────────────────────────────

function _artBadge(label, color) {
  const cls = {
    blue:   "bg-blue-100 text-blue-700",
    green:  "bg-green-100 text-green-700",
    yellow: "bg-yellow-100 text-yellow-800",
    red:    "bg-red-100 text-red-700",
    gray:   "bg-gray-100 text-gray-600",
    indigo: "bg-indigo-100 text-indigo-700",
    purple: "bg-purple-100 text-purple-700",
  }[color] || "bg-gray-100 text-gray-600";
  return <span className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide ${cls}`}>{label}</span>;
}

function _artFileList(files, tag, color) {
  if (!files?.length) return null;
  return files.map((f, i) => (
    <li key={i} className="flex items-center gap-2 px-2 py-1 hover:bg-gray-50 rounded">
      {_artBadge(tag, color)}
      <span className="font-mono text-[11px] text-gray-700 break-all">
        {typeof f === "string" ? f : (f?.path || f?.file || f?.filename || JSON.stringify(f))}
      </span>
    </li>
  ));
}

function ClassifyArtifact({ p }) {
  const riskPct   = Math.round((p.risk_score || 0) * 100);
  const riskColor = riskPct >= 70 ? "bg-red-500" : riskPct >= 40 ? "bg-yellow-500" : "bg-green-500";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {p.type       && _artBadge(p.type,       p.type === "bug" ? "red" : "indigo")}
        {p.complexity && _artBadge(p.complexity,  "blue")}
        {p.hint && p.hint !== p.complexity && _artBadge(p.hint, "gray")}
      </div>
      {p.risk_score != null && (
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">Risk Score</span>
            <span className="text-xs font-semibold text-gray-700">{riskPct}%</span>
          </div>
          <div className="h-2 w-full bg-gray-200 rounded-full overflow-hidden">
            <div className={`h-full rounded-full ${riskColor}`} style={{ width: `${riskPct}%` }} />
          </div>
        </div>
      )}
    </div>
  );
}

function AnalyzeArtifact({ p }) {
  const riskRaw   = p.regression_risk;
  const riskLevel = (typeof riskRaw === "string" ? riskRaw : riskRaw?.level || riskRaw?.score || "").toLowerCase();
  const riskColor = { low: "green", medium: "yellow", high: "red" }[riskLevel] || "gray";
  const [showSpec, setShowSpec]           = useState(false);
  const [showCodePath, setShowCodePath]   = useState(false);
  const [showHypotheses, setShowHypotheses] = useState(false);

  // Bug RCA fields
  const hypotheses = Array.isArray(p.hypotheses) ? p.hypotheses : [];

  return (
    <div className="space-y-3">
      {/* ── Bug RCA: root cause ────────────────────────── */}
      {p.root_cause && (
        <div className="p-3 bg-red-50 rounded border border-red-100">
          <p className="text-[10px] font-semibold text-red-700 uppercase tracking-wide mb-1">Root Cause</p>
          <p className="text-[12px] text-red-900 leading-relaxed">{p.root_cause}</p>
        </div>
      )}

      {/* ── Bug RCA: hypotheses ────────────────────────── */}
      {hypotheses.length > 0 && (
        <div>
          <button onClick={() => setShowHypotheses(!showHypotheses)} className="text-[10px] text-indigo-600 hover:underline flex items-center gap-1 cursor-pointer">
            {showHypotheses ? <ChevronDown size={10}/> : <ChevronRight size={10}/>}
            {showHypotheses ? "Hide" : "Show"} hypotheses ({hypotheses.length})
          </button>
          {showHypotheses && (
            <ol className="mt-2 space-y-1">
              {hypotheses.map((h, i) => (
                <li key={i} className="flex items-start gap-2 text-[11px] text-gray-700">
                  <span className="flex-shrink-0 w-4 h-4 rounded-full bg-yellow-100 text-yellow-700 text-[10px] font-bold flex items-center justify-center mt-0.5">{i + 1}</span>
                  {typeof h === "string" ? h : (h.hypothesis || h.description || JSON.stringify(h))}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      {/* ── Feature analysis: files to change ─────────── */}
      {(p.files_to_change?.length > 0 || p.new_files_needed?.length > 0 || p.affected_files?.length > 0) && (
        <div>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide mb-1">File Scope</p>
          <ul className="space-y-0.5">
            {_artFileList(p.files_to_change,  "EDIT",     "blue")}
            {_artFileList(p.new_files_needed, "NEW",      "green")}
            {_artFileList(p.affected_files,   "AFFECTED", "red")}
          </ul>
        </div>
      )}

      {/* ── Feature analysis: sub-tasks ───────────────── */}
      {p.sub_tasks?.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide mb-1">Sub-Tasks</p>
          <ul className="space-y-0.5">
            {p.sub_tasks.map((t, i) => (
              <li key={i} className="flex items-start gap-1.5 text-[11px] text-gray-700">
                <CheckCircle2 size={11} className="mt-0.5 flex-shrink-0 text-green-400" />
                {typeof t === "string" ? t : JSON.stringify(t)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {riskLevel && (
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">Regression Risk</span>
          {_artBadge(riskLevel, riskColor)}
        </div>
      )}

      {/* ── Bug RCA: code path ────────────────────────── */}
      {p.code_path && (
        <div>
          <button onClick={() => setShowCodePath(!showCodePath)} className="text-[10px] text-indigo-600 hover:underline flex items-center gap-1 cursor-pointer">
            {showCodePath ? <ChevronDown size={10}/> : <ChevronRight size={10}/>}
            {showCodePath ? "Hide" : "Show"} code path
          </button>
          {showCodePath && (
            <pre className="mt-2 text-[11px] text-gray-700 bg-gray-50 p-2 rounded max-h-40 overflow-y-auto whitespace-pre-wrap">{p.code_path}</pre>
          )}
        </div>
      )}

      {/* ── Bug RCA: missing test ─────────────────────── */}
      {p.missing_test && (
        <div className="p-2 bg-yellow-50 rounded border border-yellow-100 text-[11px] text-yellow-800">
          <span className="font-semibold">Missing Test: </span>{p.missing_test}
        </div>
      )}

      {/* ── Feature: implementation spec ──────────────── */}
      {p.implementation_spec && (
        <div>
          <button onClick={() => setShowSpec(!showSpec)} className="text-[10px] text-indigo-600 hover:underline flex items-center gap-1 cursor-pointer">
            {showSpec ? <ChevronDown size={10}/> : <ChevronRight size={10}/>}
            {showSpec ? "Hide" : "Show"} implementation spec
          </button>
          {showSpec && (
            <pre className="mt-2 text-[11px] text-gray-700 bg-gray-50 p-2 rounded max-h-48 overflow-y-auto whitespace-pre-wrap">
              {typeof p.implementation_spec === "string" ? p.implementation_spec : JSON.stringify(p.implementation_spec, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function DesignArtifact({ p }) {
  const [showExtra, setShowExtra]   = useState(false);
  const [showRca, setShowRca]       = useState(false);
  // Support both feature design schema and bug fix schema
  const approach      = p.solution_approach || p.fix_approach || p.fix_description || "";
  const approachLabel = p.solution_approach ? "Solution Approach" : "Fix Approach";
  const plan          = Array.isArray(p.implementation_plan) ? p.implementation_plan : [];
  const codeChanges   = Array.isArray(p.code_changes) ? p.code_changes : [];
  const regressionRisk = p.regression_risk || "";
  const riskColor      = { low: "green", medium: "yellow", high: "red" }[String(regressionRisk).toLowerCase()] || "gray";
  return (
    <div className="space-y-3">
      {approach && (
        <div className="p-3 bg-blue-50 rounded border border-blue-100">
          <p className="text-[10px] font-semibold text-blue-700 uppercase tracking-wide mb-1">{approachLabel}</p>
          <p className="text-[12px] text-blue-900 leading-relaxed">{approach}</p>
        </div>
      )}

      {/* Bug-specific: root cause analysis */}
      {p.root_cause_analysis && (
        <div>
          <button onClick={() => setShowRca(!showRca)} className="text-[10px] text-indigo-600 hover:underline flex items-center gap-1 cursor-pointer">
            {showRca ? <ChevronDown size={10}/> : <ChevronRight size={10}/>}
            {showRca ? "Hide" : "Show"} root cause analysis
          </button>
          {showRca && (
            <div className="mt-2 p-2 bg-red-50 rounded border border-red-100 text-[11px] text-red-900 whitespace-pre-wrap leading-relaxed">
              {p.root_cause_analysis}
            </div>
          )}
        </div>
      )}

      {regressionRisk && (
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">Regression Risk</span>
          {_artBadge(String(regressionRisk), riskColor)}
        </div>
      )}

      {plan.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide mb-1">Implementation Plan</p>
          <ol className="space-y-1">
            {plan.map((step, i) => (
              <li key={i} className="flex items-start gap-2 text-[11px] text-gray-700">
                <span className="flex-shrink-0 w-4 h-4 rounded-full bg-indigo-100 text-indigo-700 text-[10px] font-bold flex items-center justify-center mt-0.5">{i + 1}</span>
                {typeof step === "string" ? step : JSON.stringify(step)}
              </li>
            ))}
          </ol>
        </div>
      )}
      {codeChanges.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide mb-1">Code Changes ({codeChanges.length} files)</p>
          <ul className="space-y-1">
            {codeChanges.map((c, i) => (
              <li key={i} className="p-2 bg-gray-50 rounded text-[11px]">
                <span className="font-mono font-semibold text-gray-800">{c.file || c.path}</span>
                {(c.description || c.approach || c.change) && (
                  <p className="text-gray-600 mt-0.5">{c.description || c.approach || c.change}</p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {(p.testing_strategy || p.rollback_strategy) && (
        <div>
          <button onClick={() => setShowExtra(!showExtra)} className="text-[10px] text-indigo-600 hover:underline flex items-center gap-1 cursor-pointer">
            {showExtra ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
            {showExtra ? "Hide" : "Show"} testing &amp; rollback
          </button>
          {showExtra && (
            <div className="mt-2 space-y-1.5">
              {p.testing_strategy  && <div className="p-2 bg-gray-50 rounded text-[11px] text-gray-700"><span className="font-semibold">Testing: </span>{p.testing_strategy}</div>}
              {p.rollback_strategy && <div className="p-2 bg-gray-50 rounded text-[11px] text-gray-700"><span className="font-semibold">Rollback: </span>{p.rollback_strategy}</div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SecurityIssueList({ items, color = "orange" }) {
  const colorMap = {
    red:    { text: "text-red-700",    icon: "text-red-500"    },
    orange: { text: "text-orange-700", icon: "text-orange-500" },
    yellow: { text: "text-yellow-800", icon: "text-yellow-500" },
    blue:   { text: "text-blue-700",   icon: "text-blue-500"   },
    purple: { text: "text-purple-700", icon: "text-purple-500" },
  };
  const c = colorMap[color] || colorMap.orange;
  return (
    <ul className="space-y-0.5">
      {items.map((s, i) => (
        <li key={i} className={`flex items-start gap-1.5 text-[11px] ${c.text}`}>
          <ShieldAlert size={11} className={`mt-0.5 flex-shrink-0 ${c.icon}`} />
          {typeof s === "string" ? s : JSON.stringify(s)}
        </li>
      ))}
    </ul>
  );
}

function ReviewArtifact({ p }) {
  const [showPerFile, setShowPerFile]   = useState(false);
  const [showSecurity, setShowSecurity] = useState(true);
  const score          = p.score ?? p.review_score;
  const approved       = p.approved ?? p.decision === "approved";
  const critical       = p.critical_issues || p.blocking_issues || p.blockers || [];
  const security       = p.security_issues || [];
  const checkmarx      = p.checkmarx_issues || [];
  const sonar          = p.sonar_issues || [];
  const pmd            = p.pmd_issues || [];
  const suggestions    = p.suggestions || p.improvements || [];
  const perFile        = p.per_file || [];
  const hasSecurityFindings = security.length > 0 || checkmarx.length > 0 || sonar.length > 0 || pmd.length > 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 flex-wrap">
        {score != null && (
          <div className="flex items-center gap-2">
            <div className="h-2 w-24 bg-gray-200 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full ${score >= 7 ? "bg-green-500" : score >= 4 ? "bg-yellow-500" : "bg-red-500"}`}
                style={{ width: `${Math.min(100, score * 10)}%` }}
              />
            </div>
            <span className="text-xs font-semibold text-gray-700">{score}/10</span>
          </div>
        )}
        {_artBadge(approved ? "Approved" : "Changes Requested", approved ? "green" : "red")}
      </div>

      {p.summary && <p className="text-[12px] text-gray-700 leading-relaxed">{p.summary}</p>}

      {critical.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-red-600 uppercase tracking-wide mb-1">Critical Issues</p>
          <ul className="space-y-0.5">
            {critical.map((b, i) => (
              <li key={i} className="flex items-start gap-1.5 text-[11px] text-red-700">
                <XCircle size={11} className="mt-0.5 flex-shrink-0" />
                {typeof b === "string" ? b : JSON.stringify(b)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Security Review Panel ───────────────────────────── */}
      <div className={`rounded-lg border p-3 ${hasSecurityFindings ? "border-orange-200 bg-orange-50/40" : "border-green-200 bg-green-50/30"}`}>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-1.5">
            {hasSecurityFindings
              ? <ShieldAlert size={13} className="text-orange-600 flex-shrink-0" />
              : <Shield size={13} className="text-green-600 flex-shrink-0" />
            }
            <span className="text-[10px] font-semibold text-gray-700 uppercase tracking-wide">Security Review</span>
            <span className={`ml-1 px-1.5 py-0.5 rounded text-[9px] font-semibold ${
              hasSecurityFindings ? "bg-orange-100 text-orange-700" : "bg-green-100 text-green-700"
            }`}>
              {hasSecurityFindings ? "Issues Found" : "Clean"}
            </span>
          </div>
          <button
            onClick={() => setShowSecurity(!showSecurity)}
            className="text-[10px] text-indigo-600 hover:underline flex items-center gap-0.5 cursor-pointer"
          >
            {showSecurity ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
            {showSecurity ? "Hide" : "Show"}
          </button>
        </div>

        {showSecurity && (
          <div className="space-y-2.5">
            {checkmarx.length > 0 && (
              <div>
                <p className="text-[9px] font-semibold text-red-600 uppercase tracking-wider mb-1">CheckMarx / OWASP</p>
                <SecurityIssueList items={checkmarx} color="red" />
              </div>
            )}
            {sonar.length > 0 && (
              <div>
                <p className="text-[9px] font-semibold text-purple-600 uppercase tracking-wider mb-1">SonarQube</p>
                <SecurityIssueList items={sonar} color="purple" />
              </div>
            )}
            {pmd.length > 0 && (
              <div>
                <p className="text-[9px] font-semibold text-blue-600 uppercase tracking-wider mb-1">PMD</p>
                <SecurityIssueList items={pmd} color="blue" />
              </div>
            )}
            {security.length > 0 && (
              <div>
                <p className="text-[9px] font-semibold text-orange-600 uppercase tracking-wider mb-1">PCI / General</p>
                <SecurityIssueList items={security} color="orange" />
              </div>
            )}
            {!hasSecurityFindings && (
              <p className="text-[11px] text-green-700 flex items-center gap-1.5">
                <Shield size={11} className="flex-shrink-0" />
                No PMD, CheckMarx, or SonarQube security issues detected.
              </p>
            )}
          </div>
        )}
      </div>

      {suggestions.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-yellow-700 uppercase tracking-wide mb-1">Suggestions</p>
          <ul className="space-y-0.5">
            {suggestions.map((s, i) => (
              <li key={i} className="flex items-start gap-1.5 text-[11px] text-yellow-800">
                <AlertTriangle size={11} className="mt-0.5 flex-shrink-0 text-yellow-500" />
                {typeof s === "string" ? s : JSON.stringify(s)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {perFile.length > 0 && (
        <div>
          <button onClick={() => setShowPerFile(!showPerFile)} className="text-[10px] text-indigo-600 hover:underline flex items-center gap-1 cursor-pointer">
            {showPerFile ? <ChevronDown size={10}/> : <ChevronRight size={10}/>}
            {showPerFile ? "Hide" : "Show"} per-file ({perFile.length} files)
          </button>
          {showPerFile && (
            <ul className="mt-2 space-y-1 max-h-48 overflow-y-auto">
              {perFile.map((f, i) => (
                <li key={i} className="p-2 bg-gray-50 rounded text-[11px] text-gray-700">
                  {typeof f === "string" ? f : (
                    <>
                      <span className="font-mono font-semibold">{f.file || f.path || JSON.stringify(f)}</span>
                      {f.comment && <p className="text-gray-500 mt-0.5">{f.comment}</p>}
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function CrossModelReviewArtifact({ p }) {
  const severityColor = { none: "gray", low: "blue", medium: "yellow", high: "red", critical: "red" }[p.severity] || "gray";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {_artBadge(p.agreed ? "Models Agreed" : "Models Disagreed", p.agreed ? "green" : "red")}
        {p.severity && p.severity !== "none" && _artBadge(p.severity, severityColor)}
      </div>
      {p.models_used?.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide mb-1">Models</p>
          <div className="flex flex-wrap gap-1.5">
            {p.models_used.map((m, i) => (
              <span key={i} className="px-2 py-0.5 rounded bg-gray-100 text-gray-700 text-[11px] font-mono">{String(m)}</span>
            ))}
          </div>
        </div>
      )}
      {p.issues?.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-red-600 uppercase tracking-wide mb-1">Issues Found</p>
          <ul className="space-y-1">
            {p.issues.map((iss, i) => {
              if (typeof iss === "string") {
                return (
                  <li key={i} className="flex items-start gap-1.5 text-[11px] text-red-700">
                    <XCircle size={11} className="mt-0.5 flex-shrink-0" />
                    {iss}
                  </li>
                );
              }
              const catColors = {
                checkmarx: "bg-red-100 text-red-700",
                sonar:     "bg-purple-100 text-purple-700",
                pmd:       "bg-blue-100 text-blue-700",
                pci:       "bg-orange-100 text-orange-700",
                general:   "bg-gray-100 text-gray-600",
              };
              const catCls = catColors[iss.category] || catColors.general;
              return (
                <li key={i} className="p-2 bg-red-50 rounded border border-red-100">
                  <div className="flex items-center gap-1.5 mb-0.5 flex-wrap">
                    <XCircle size={11} className="text-red-500 flex-shrink-0" />
                    <span className="text-[11px] font-semibold text-red-700 font-mono">{iss.file || ""}</span>
                    {iss.category && (
                      <span className={`px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase ${catCls}`}>{iss.category}</span>
                    )}
                    {iss.type && (
                      <span className="px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 text-[9px]">{iss.type}</span>
                    )}
                  </div>
                  {iss.issue && <p className="text-[11px] text-red-700 ml-4">{iss.issue}</p>}
                  {iss.suggestion && <p className="text-[10px] text-gray-500 ml-4 mt-0.5">Fix: {iss.suggestion}</p>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

function CodingArtifact({ p }) {
  const files = p.files || p.changed_files || [];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {p.fix_attempt != null && _artBadge(`Attempt #${p.fix_attempt}`, "blue")}
        {p.trigger     && _artBadge(p.trigger, "gray")}
      </div>
      {p.summary && <p className="text-[12px] text-gray-700 leading-relaxed">{p.summary}</p>}
      {files.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide mb-1">{files.length} file{files.length !== 1 ? "s" : ""} changed</p>
          <ul className="space-y-0.5">
            {files.map((f, i) => {
              const path   = typeof f === "string" ? f : (f?.path || f?.file || f?.filename || JSON.stringify(f));
              const isTest = typeof f === "object" && f?.is_test;
              return (
                <li key={i} className="flex items-center gap-2 px-2 py-1 hover:bg-gray-50 rounded">
                  {isTest ? _artBadge("TEST", "purple") : _artBadge("EDIT", "blue")}
                  <span className="font-mono text-[11px] text-gray-700 break-all">{path}</span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

function TestingArtifact({ p }) {
  const [showReport, setShowReport] = useState(false);
  // Artifact payload uses booleans; run.context.test_result uses counts — handle both
  const passedBool = typeof p.passed === "boolean" ? p.passed : null;
  const failedBool = typeof p.failed === "boolean" ? p.failed : null;
  const buildOk    = p.build_ok ?? p.build_status === "ok";
  const report     = p.test_report || "";
  // Count fields (from context fallback)
  const passedN    = typeof p.tests_passed === "number" ? p.tests_passed : (typeof p.passed === "number" ? p.passed : null);
  const failedN    = typeof p.tests_failed === "number" ? p.tests_failed : (typeof p.failed === "number" ? p.failed : null);
  const total      = passedN != null && failedN != null ? passedN + failedN : null;
  const pct        = total > 0 ? Math.round((passedN / total) * 100) : null;
  const errors     = p.errors || p.error_messages || [];
  return (
    <div className="space-y-3">
      {total != null && total > 0 && (
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">Test Results</span>
            <span className="text-xs font-semibold text-gray-700">{passedN}/{total} passed</span>
          </div>
          <div className="h-2.5 w-full bg-gray-200 rounded-full overflow-hidden">
            <div className={`h-full rounded-full ${pct === 100 ? "bg-green-500" : pct >= 50 ? "bg-yellow-500" : "bg-red-500"}`}
              style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {buildOk != null && _artBadge(buildOk ? "Build OK" : "Build Failed", buildOk ? "green" : "red")}
        {passedBool != null && _artBadge(passedBool ? "Tests Passed" : "Tests Failed", passedBool ? "green" : "red")}
        {passedN != null && _artBadge(`${passedN} passed`, "green")}
        {failedN != null && failedN > 0 && _artBadge(`${failedN} failed`, "red")}
      </div>
      {errors.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-red-600 uppercase tracking-wide mb-1">Errors</p>
          <ul className="space-y-1 max-h-40 overflow-y-auto">
            {errors.map((e, i) => (
              <li key={i} className="text-[11px] text-red-700 font-mono bg-red-50 px-2 py-1 rounded break-all">
                {typeof e === "string" ? e : JSON.stringify(e)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {report && (
        <div>
          <button onClick={() => setShowReport(!showReport)} className="text-[10px] text-indigo-600 hover:underline flex items-center gap-1 cursor-pointer">
            {showReport ? <ChevronDown size={10}/> : <ChevronRight size={10}/>}
            {showReport ? "Hide" : "Show"} test report
          </button>
          {showReport && (
            <pre className="mt-2 text-[11px] text-gray-700 font-mono bg-gray-50 p-2 rounded max-h-48 overflow-y-auto whitespace-pre-wrap">{report}</pre>
          )}
        </div>
      )}
    </div>
  );
}

function CommitArtifact({ p }) {
  const mrUrl = p.mr_url || p.pr_url;
  const [copiedSha, setCopiedSha] = useState(false);
  const [copiedBranch, setCopiedBranch] = useState(false);
  const copyText = (text, setter) => {
    navigator.clipboard.writeText(text).then(() => { setter(true); setTimeout(() => setter(false), 1500); }).catch(() => {});
  };
  return (
    <div className="space-y-3">
      {p.branch && (
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">Branch</span>
          <span className="font-mono text-xs bg-gray-100 px-2 py-0.5 rounded text-gray-800">{p.branch}</span>
          <button onClick={() => copyText(p.branch, setCopiedBranch)} title="Copy branch name" className="text-gray-400 hover:text-gray-600 cursor-pointer">
            {copiedBranch ? <Check size={11} className="text-green-500" /> : <Copy size={11} />}
          </button>
        </div>
      )}
      {p.commit_sha && (
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">Commit</span>
          <span className="font-mono text-xs bg-gray-100 px-2 py-0.5 rounded text-gray-800">{String(p.commit_sha).slice(0, 10)}</span>
          <button onClick={() => copyText(String(p.commit_sha), setCopiedSha)} title="Copy full commit SHA" className="text-gray-400 hover:text-gray-600 cursor-pointer">
            {copiedSha ? <Check size={11} className="text-green-500" /> : <Copy size={11} />}
          </button>
        </div>
      )}
      {p.pr_number && (
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">MR #</span>
          <span className="font-mono text-xs bg-gray-100 px-2 py-0.5 rounded text-gray-800">{p.pr_number}</span>
        </div>
      )}
      {mrUrl && (
        <a href={mrUrl} target="_blank" rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-xs text-indigo-600 hover:text-indigo-800 font-medium">
          <ExternalLink size={12} /> Open Merge Request
        </a>
      )}
    </div>
  );
}

// FallbackArtifact — the generic renderer for ANY stage key with no dedicated
// view. Never throws; renders the raw payload as readable JSON. This is what keeps
// HISTORICAL runs (whose stored events carry now-removed stages) first-class: an
// unknown/legacy stage key always displays, never blanks.
function DefaultPayloadView({ p }) {
  return (
    <pre className="text-xs bg-gray-50 p-3 rounded border border-gray-200 overflow-auto whitespace-pre-wrap">
      {JSON.stringify(p, null, 2)}
    </pre>
  );
}
const FallbackArtifact = DefaultPayloadView;

// Per-stage artifact renderers. The merged-planner PLAN node, MANIFEST_VALIDATION,
// and the three-phase CLI engine's IMPLEMENT/REVIEW nodes are NOT here — they are
// rendered by dedicated components (PlanningArtifactView / ManifestValidationPanel /
// DiffApprovalPanel / ReviewVerdictView) in StageArtifactDrawer's special-case below,
// which need run/runId/events rather than a bare payload.
//
// CROSS_MODEL_REVIEW and FIXING are REMOVED stages (review teeth consolidated into
// REVIEW_GATE; standalone FIXING absorbed into CODING). They are intentionally kept
// here ONLY so historical artifacts stored under those keys still render richly —
// the live backend manifest never surfaces them, so the new timeline can't click them.
const STAGE_RENDERERS = {
  CLASSIFYING:        ClassifyArtifact,
  ANALYZING:          AnalyzeArtifact,    // legacy split-mode runs; merged PLAN handled below
  DESIGNING:          DesignArtifact,     // legacy split-mode runs; merged PLAN handled below
  REVIEWING:          ReviewArtifact,
  REVIEW_GATE:        ReviewArtifact,
  CROSS_MODEL_REVIEW: CrossModelReviewArtifact,  // historical only
  CODING:             CodingArtifact,
  FIXING:             CodingArtifact,             // historical only
  APPLYING:           CodingArtifact,
  TESTING:            TestingArtifact,
  TEST_VERIFY:        TestingArtifact,
  COMMITTING:         CommitArtifact,
  MR_CREATION:        CommitArtifact,
};

// ── Agentic loop transcript (P1-B) ────────────────────────────
// Renders the loop_transcript an agentic stage (CODING/FIXING/TESTING/ANALYZING or
// BASELINE_BUILD agent-fix) writes into its artifact payload. Pure presentational —
// the artifact endpoint already returns the key; no backend change.

function LoopTranscriptView({ t }) {
  const [expandedTurns, setExpandedTurns] = useState(new Set());
  if (!t || typeof t !== "object") return null;
  const usage   = t.usage || {};
  const byModel = usage.by_model || {};
  const turns   = Array.isArray(t.turns) ? t.turns : [];
  const applied = Array.isArray(t.applied_files) ? t.applied_files : [];
  const toggleTurn = (key) => setExpandedTurns(prev => {
    const next = new Set(prev);
    next.has(key) ? next.delete(key) : next.add(key);
    return next;
  });
  const inTok   = Number(usage.input_tokens || 0);
  const outTok  = Number(usage.output_tokens || 0);
  const statusOk = t.status === "completed";

  return (
    <div className="mt-4 border-t border-gray-200 pt-3">
      <div className="flex items-center gap-1.5 mb-2">
        <RotateCcw size={13} className="text-indigo-500" />
        <h4 className="text-xs font-semibold text-gray-800">Agentic loop</h4>
        <span className={`ml-auto px-1.5 py-0.5 rounded text-[10px] font-medium ${
          statusOk ? "bg-green-100 text-green-700" : "bg-amber-100 text-amber-700"}`}>
          {t.status || "?"}
        </span>
      </div>

      {/* Summary line */}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-600 mb-2">
        <span><strong>{t.rounds ?? turns.length}</strong> rounds</span>
        <span><strong>{applied.length}</strong> file(s) edited</span>
        <span>{(inTok + outTok).toLocaleString()} tok ({inTok.toLocaleString()} in / {outTok.toLocaleString()} out)</span>
      </div>

      {t.reason && (
        <p className="text-[11px] text-gray-500 italic mb-2 break-words">{t.reason}</p>
      )}

      {/* Per-model token summary */}
      {Object.keys(byModel).length > 0 && (
        <div className="mb-2">
          <table className="w-full text-[10px] text-gray-600">
            <thead>
              <tr className="text-gray-400 text-left">
                <th className="font-medium pb-0.5">Model</th>
                <th className="font-medium pb-0.5 text-right">In</th>
                <th className="font-medium pb-0.5 text-right">Out</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(byModel).map(([m, v], _i) => (
                <tr key={m} className="border-t border-gray-100">
                  <td className="py-0.5 pr-2 font-mono break-all">{m}</td>
                  <td className="py-0.5 text-right">{Number(v?.in || 0).toLocaleString()}</td>
                  <td className="py-0.5 text-right">{Number(v?.out || 0).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Applied files */}
      {applied.length > 0 && (
        <div className="mb-2">
          <p className="text-[10px] text-gray-400 mb-0.5">Edited files</p>
          <div className="flex flex-wrap gap-1">
            {applied.map((f, i) => (
              <span key={`${String(f)}-${i}`} className="px-1.5 py-0.5 bg-gray-100 rounded text-[10px] font-mono text-gray-600 break-all">{String(f)}</span>
            ))}
          </div>
        </div>
      )}

      {/* Per-round transcript */}
      {turns.length > 0 && (
        <div className="space-y-1.5">
          {turns.map((turn, i) => (
            <div key={`round-${turn.round ?? i}`} className="rounded border border-gray-100 bg-gray-50 p-1.5">
              <div className="flex items-center gap-2 text-[10px] text-gray-500 mb-0.5">
                <span className="font-semibold text-gray-700">#{turn.round ?? i + 1}</span>
                <span className="font-mono">{turn.model || "?"}</span>
                {Array.isArray(turn.tools) && turn.tools.length > 0 && (
                  <span className="text-indigo-500">{turn.tools.join(", ")}</span>
                )}
                {turn.budget_breached && (
                  <span className="ml-auto text-red-500 font-medium">budget breached</span>
                )}
              </div>
              {turn.text && (() => {
                const key = `round-${turn.round ?? i}`;
                const isExpanded = expandedTurns.has(key);
                const text = String(turn.text);
                const isLong = text.length > 200;
                return (
                  <div>
                    <p className={`text-[10px] text-gray-600 whitespace-pre-wrap break-words ${isLong && !isExpanded ? "line-clamp-4" : ""}`}>{text}</p>
                    {isLong && (
                      <button onClick={() => toggleTurn(key)} className="text-[10px] text-indigo-500 hover:underline cursor-pointer mt-0.5">
                        {isExpanded ? "Show less ↑" : "Show more ↓"}
                      </button>
                    )}
                  </div>
                );
              })()}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Manifest validation banner (compact, self-fetching) ────────
// Used both on the SUSPENDED-at-PLAN banner and inside the PLAN drawer.
function ManifestValidationBanner({ runId }) {
  const [artifact, setArtifact] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    setLoading(true);
    apiFetch(`${API}/sdlc/runs/${runId}/stages/MANIFEST_VALIDATION/artifact`)
      .then(r => (r.ok ? r.json() : null))
      .then(data => { if (!cancelled) { setArtifact(data?.payload ?? data); setLoading(false); } })
      .catch(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [runId]);

  if (loading) return (
    <div className="h-8 bg-gray-100 rounded animate-pulse w-48" />
  );
  if (!artifact) return null;
  return <ManifestValidationPanel artifact={artifact} />;
}

// ── Review verdict view (REVIEW node) ──────────────────────────
// REVIEW has no dedicated stage-artifact row (only PLAN/CLASSIFYING/
// MANIFEST_VALIDATION/VERIFIED_DIFF/APPLYING/TEST_VERIFY/COMMITTING do) — the
// Opus diff-review verdict lives on the REVIEW run event(s)
// (agents/sdlc_pipeline.py _run_review_phase: data.approved, data.blocking,
// output=notes). Reads the already-fetched run events, no extra round-trip.
function ReviewVerdictView({ events }) {
  const reviewEvents = (events || []).filter(e => e.stage === "REVIEW");
  if (reviewEvents.length === 0) {
    return <p className="text-sm text-gray-400 text-center mt-8">No review verdict recorded yet.</p>;
  }
  const latest = reviewEvents[reviewEvents.length - 1];
  const approved = latest.data?.approved;
  const blocking = latest.data?.blocking ?? 0;
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {approved
          ? <CheckCircle2 size={16} className="text-green-500" />
          : <XCircle size={16} className="text-red-500" />}
        <span className="font-medium text-gray-800 text-sm">Opus Diff Review</span>
        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${approved ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
          {approved ? 'APPROVED' : 'BLOCKED'}
        </span>
      </div>
      {blocking > 0 && (
        <p className="text-xs text-red-600">{blocking} blocking issue(s) raised.</p>
      )}
      {latest.output && (
        <div className="bg-gray-50 border border-gray-200 rounded p-2">
          <p className="text-xs font-semibold text-gray-500 mb-1">NOTES</p>
          <p className="text-xs text-gray-700 whitespace-pre-wrap">{latest.output}</p>
        </div>
      )}
      {reviewEvents.length > 1 && (
        <p className="text-[11px] text-gray-400">{reviewEvents.length} review round(s) recorded.</p>
      )}
    </div>
  );
}

// ── Stage Artifact Drawer ─────────────────────────────────────

function StageArtifactDrawer({ runId, stage, stageLabel, run, events, onClose }) {
  const [artifact, setArtifact] = useState(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(null);

  const scrollContainerRef = useRef(null);
  useEffect(() => {
    if (scrollContainerRef.current) scrollContainerRef.current.scrollTop = 0;
  }, [stage]);

  useEffect(() => {
    if (!runId || !stage) return;
    setLoading(true); setFetchError(null);
    apiFetch(`${API}/sdlc/runs/${runId}/stages/${stage}/artifact`)
      .then(r => {
        if (r.status === 404) { setArtifact(null); setLoading(false); return null; }
        if (!r.ok) throw new Error(`Failed to load artifact (${r.status})`);
        return r.json();
      })
      .then(data => { if (data !== null) { setArtifact(data); setLoading(false); } })
      .catch(e => { setFetchError(e.message || "Failed to load artifact"); setLoading(false); });
  }, [runId, stage]);

  // ANALYZING and DESIGNING have no stored artifact — fall back to run.context.
  // Bug runs: analysis = ctx.rca (root cause analysis), design = ctx.fix.
  // Feature runs: analysis = ctx.analysis, design = ctx.design.
  const ctx = run?.context || {};
  const contextFallback = !artifact && !loading ? (
    stage === "ANALYZING" ? (ctx.analysis || ctx.rca || null) :
    stage === "DESIGNING" ? (ctx.design || ctx.fix || null) :
    null
  ) : null;

  const title = stageLabel || stage;

  // Dedicated artifact views for the artifact-oriented stages. PLAN (merged) and
  // legacy ANALYZING/DESIGNING all render through PlanningArtifactView, which
  // self-fetches (PLAN artifact → ANALYZING+DESIGNING → run.context). MANIFEST_
  // VALIDATION renders the structured pass/reject panel over the fetched artifact.
  // IMPLEMENT/REVIEW (three-phase CLI engine) have no dedicated stage-artifact row
  // of their own — IMPLEMENT's real output IS the VERIFIED_DIFF artifact (reuse
  // DiffApprovalPanel read-only) and REVIEW's verdict lives on REVIEW run events
  // (reuse ReviewVerdictView) — see store/sdlc_artifacts.STAGE_DAG / MANDATORY_STAGES.
  const PLANNING_STAGES = ["PLAN", "ANALYZING", "DESIGNING"];
  const isPlanningStage  = PLANNING_STAGES.includes(stage);
  const isManifestVal    = stage === "MANIFEST_VALIDATION";
  const isImplementStage = stage === "IMPLEMENT";
  const isReviewStage    = stage === "REVIEW";
  // GOVERNANCE_REVIEW (EA/IS/DPDP pluggable skills over the diff) has its own
  // dedicated report view — see agents/sdlc_governance/engine.py::render_report
  // and GET /sdlc/runs/{id}/governance.
  const isGovernanceStage = stage === "GOVERNANCE_REVIEW" || stage === "AWAITING_GOVERNANCE_APPROVAL";

  return (
    <>
    {/* Backdrop — clicking outside closes the drawer */}
    <div className="fixed inset-0 bg-black/20 z-40" onClick={onClose} />
    <div className="fixed inset-y-0 right-0 w-96 bg-white shadow-2xl z-50 flex flex-col border-l border-gray-200">
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
        <div>
          <h3 className="font-semibold text-gray-900 text-sm">{title}</h3>
          {artifact && (
            <p className="text-xs text-gray-400 mt-0.5">
              v{artifact.version} · {artifact.status} · {artifact.producer}
            </p>
          )}
          {contextFallback && (
            <p className="text-[10px] text-blue-500 mt-0.5">from run context</p>
          )}
        </div>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 cursor-pointer" title="Close">
          <XIcon size={18} />
        </button>
      </div>
      <div ref={scrollContainerRef} className="flex-1 overflow-auto p-4">
        {isPlanningStage ? (
          /* Self-fetching artifact view — its own loading + context fallback.
             The MANIFEST_VALIDATION sub-check runs INSIDE PLAN (no separate
             stepper node — see store/sdlc_stage_manifest.py aliases), so its
             verdict is surfaced here as a sibling section, not a separate click. */
          <div className="space-y-3">
            <PlanningArtifactView run={run} runId={runId} />
            <ManifestValidationBanner runId={runId} />
          </div>
        ) : isImplementStage ? (
          /* IMPLEMENT's real output is the VERIFIED_DIFF artifact — reuse the
             same read-only panel shown at the HITL gate. */
          <DiffApprovalPanel run={run} />
        ) : isReviewStage ? (
          <ReviewVerdictView events={events} />
        ) : isGovernanceStage ? (
          <GovernanceReviewPanel
            runId={runId}
            run={run}
            repo={run?.repo}
            productId={run?.context?.product_id}
            onRefresh={onClose}
          />
        ) : loading ? (
          <div className="flex items-center justify-center h-32">
            <Loader2 size={24} className="animate-spin text-gray-400" />
          </div>
        ) : fetchError ? (
          <div className="flex flex-col items-center justify-center h-32 gap-2 text-center px-4">
            <XCircle size={20} className="text-red-400" />
            <p className="text-sm text-red-600 font-medium">Failed to load artifact</p>
            <p className="text-xs text-red-400">{fetchError}</p>
          </div>
        ) : artifact ? (
          <div>
            {artifact.reason && (
              <div className="mb-3 p-2 bg-amber-50 border border-amber-200 rounded text-xs text-amber-800">
                <strong>Reason:</strong> {artifact.reason}
              </div>
            )}
            {isManifestVal ? (
              <ManifestValidationPanel artifact={artifact.payload ?? artifact} />
            ) : (() => {
              const Renderer = STAGE_RENDERERS[stage] ?? FallbackArtifact;
              return <Renderer p={artifact.payload ?? artifact} />;
            })()}
            {/* P1-B: an agentic stage co-stores a loop_transcript alongside its
                normal payload — render it as an extra section, never a replacement. */}
            {(() => {
              const t = (artifact.payload ?? artifact)?.loop_transcript;
              return t ? <LoopTranscriptView t={t} /> : null;
            })()}
          </div>
        ) : contextFallback ? (
          <div>
            {(() => {
              const Renderer = STAGE_RENDERERS[stage] ?? FallbackArtifact;
              return <Renderer p={contextFallback} />;
            })()}
          </div>
        ) : (
          <p className="text-sm text-gray-400 text-center mt-8">No artifact found for this stage.</p>
        )}
      </div>
    </div>
    </>
  );
}

// ── Stage Action Panel (retry / go-back / waive on suspended runs) ─

// Three-phase CLI engine (gate-reorder, 2026-07-02): both run types share one
// pre-SM head (NORMALIZE → CLASSIFYING → PLAN) + the shared SM tail (IMPLEMENT →
// REVIEW → COMMITTING) — MUST mirror store/sdlc_artifacts.stage_sequence_for()
// exactly, since resume_from_stage() 400s on any target_stage not in that list.
const SHARED_SM_STAGE_ORDER = ["IMPLEMENT", "REVIEW", "TEST_VERIFY", "COMMITTING"];
// GOVERNANCE_SCAN appended at the tail (2026-07-24 governance end-gate):
// resumable so a run SUSPENDED at GOVERNANCE_SCAN can be re-driven via
// resume_from_stage(target_stage="GOVERNANCE_SCAN"). Intentionally NOT added
// to StageActionPanel's MANDATORY set below — governance stays optional/waivable.
const RESUMABLE_STAGE_ORDER_BY_TYPE = {
  feature: ["NORMALIZE", "CLASSIFYING", "PLAN", ...SHARED_SM_STAGE_ORDER, "GOVERNANCE_SCAN"],
  bug:     ["NORMALIZE", "CLASSIFYING", "PLAN", ...SHARED_SM_STAGE_ORDER, "GOVERNANCE_SCAN"],
};
function resumableStageOrder(runType) {
  return RESUMABLE_STAGE_ORDER_BY_TYPE[(runType || "feature").toLowerCase()]
      || RESUMABLE_STAGE_ORDER_BY_TYPE.feature;
}

function StageActionPanel({ runId, stage, runState, runType, onClose, onDone }) {
  const { toast } = useToast();
  const [mode, setMode]         = useState("retry");
  const [feedback, setFeedback] = useState("");
  const [reason, setReason]     = useState("");
  const [loading, setLoading]   = useState(false);

  const stageOrder      = resumableStageOrder(runType);
  const currentStageIdx = stageOrder.indexOf(stage);
  const goBackStages    = currentStageIdx > 0 ? stageOrder.slice(0, currentStageIdx) : [];
  const [backStage, setBackStage] = useState(goBackStages[goBackStages.length - 1] || stage);

  const MANDATORY = new Set(["CLASSIFYING", "IMPLEMENT", "COMMITTING"]); // mirrors store/sdlc_artifacts.MANDATORY_STAGES
  const isMandatory = MANDATORY.has(stage);

  async function handleSubmit() {
    if ((mode === "waive" || mode === "override") && !reason.trim()) {
      toast.error("Reason is required for waive/override"); return;
    }
    if (mode === "go_back" && goBackStages.length === 0) {
      toast.error("No earlier stage to go back to from " + stage); return;
    }
    setLoading(true);
    try {
      const effectiveTarget = mode === "go_back" ? backStage : stage;
      const body = { target_stage: effectiveTarget, mode, feedback: feedback || undefined, reason: reason || undefined };
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Resume failed");
      } else {
        toast.success(`Resume (${mode}) enqueued`);
        onDone && onDone();
        onClose && onClose();
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="border border-gray-200 rounded-lg p-4 bg-gray-50 mt-2">
      <div className="flex items-center justify-between mb-3">
        <h4 className="font-semibold text-gray-800 text-sm">Stage Actions — {stage}</h4>
        {onClose && <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><XIcon size={14}/></button>}
      </div>

      {/* Mode selector */}
      <div className="flex gap-2 mb-3 flex-wrap">
        {["retry","go_back"].map(m => (
          <button key={m} onClick={() => setMode(m)}
            className={`px-2 py-1 rounded text-xs font-medium border ${mode===m ? "bg-blue-600 text-white border-blue-600" : "bg-white text-gray-600 border-gray-300 hover:border-blue-400"}`}>
            {m === "retry" ? "Retry" : "Go Back"}
          </button>
        ))}
        {!isMandatory && (
          <button onClick={() => setMode("waive")}
            className={`px-2 py-1 rounded text-xs font-medium border ${mode==="waive" ? "bg-amber-600 text-white border-amber-600" : "bg-white text-gray-600 border-gray-300 hover:border-amber-400"}`}>
            Waive
          </button>
        )}
        {isMandatory && (
          <span className="px-2 py-1 rounded text-xs text-gray-400 border border-gray-200 bg-gray-100 flex items-center gap-1">
            🔒 Mandatory — cannot be waived
          </span>
        )}
      </div>

      {/* Go Back — stage selector */}
      {mode === "go_back" && goBackStages.length > 0 && (
        <div className="mb-3">
          <label className="text-xs text-gray-600 mb-1 block font-medium">Go back to stage:</label>
          <select
            value={backStage}
            onChange={e => setBackStage(e.target.value)}
            className="w-full text-xs border border-gray-300 rounded p-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400 bg-white">
            {goBackStages.map(s => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
      )}
      {mode === "go_back" && goBackStages.length === 0 && (
        <p className="text-xs text-amber-600 mb-3 bg-amber-50 border border-amber-200 rounded p-2">
          No earlier stages available from <strong>{stage}</strong>. Use Retry to re-run this stage instead.
        </p>
      )}

      {/* Feedback / reason */}
      {(mode === "retry" || mode === "go_back") && (
        <textarea
          value={feedback}
          onChange={e => setFeedback(e.target.value)}
          placeholder="Optional: describe what to fix or change..."
          className="w-full text-xs border border-gray-300 rounded p-2 mb-3 resize-none h-20 focus:outline-none focus:ring-1 focus:ring-blue-400"
        />
      )}
      {mode === "waive" && (
        <textarea
          value={reason}
          onChange={e => setReason(e.target.value)}
          placeholder="Required: reason for waiving this gate..."
          className="w-full text-xs border border-amber-300 rounded p-2 mb-3 resize-none h-20 focus:outline-none focus:ring-1 focus:ring-amber-400"
        />
      )}

      <button
        onClick={handleSubmit}
        disabled={loading}
        className="w-full py-1.5 rounded text-xs font-semibold text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50 flex items-center justify-center gap-1">
        {loading ? <Loader2 size={12} className="animate-spin"/> : null}
        {loading ? "Submitting…" : `Submit ${mode}`}
      </button>
    </div>
  );
}

// ── Baseline Build suspended panel (re-enter pipeline / let agent fix) ─
// BASELINE_BUILD is suspended in preflight, BEFORE any artifact-backed stage, so
// the generic StageActionPanel (retry/go_back/waive via /resume) would 400. This
// panel offers the two baseline-specific actions, both hitting the dedicated
// POST /sdlc/runs/{id}/baseline/resume re-trigger route.
function BaselineActionPanel({ runId, run, suspendReason, onClose, onDone }) {
  const { toast } = useToast();
  const [loading, setLoading] = useState(null);  // null | "self" | "skip"
  // Explicit user opt-out of TESTING+SLT on resume. Never automatic — the user must
  // check this box. Initialised from the stored context value so it reflects the
  // trigger-time choice; the user can then change it for this resume.
  const [skipTests, setSkipTests] = useState(
    (run?.context?.skip_tests) === true
  );

  async function submit({ skipCompile = false } = {}) {
    setLoading(skipCompile ? "skip" : "self");
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/baseline/resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // skip_tests=null means "keep stored context value" on the backend;
        // only send when the user explicitly changed the checkbox.
        body: JSON.stringify({
          skip_compile: skipCompile,
          skip_tests: skipTests,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Baseline resume failed");
      } else {
        toast.success(skipCompile
          ? "Compilation skipped — pipeline re-entered without building"
          : "Re-checking the baseline build — pipeline re-entered");
        onDone && onDone();
        onClose && onClose();
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(null);
    }
  }

  return (
    <div className="border border-amber-200 rounded-lg p-4 bg-amber-50 mt-2">
      <div className="flex items-center justify-between mb-2">
        <h4 className="font-semibold text-amber-900 text-sm flex items-center gap-1">
          <AlertTriangle size={14} /> Baseline build broken at HEAD
        </h4>
        {onClose && <button onClick={onClose} className="text-amber-400 hover:text-amber-600"><XIcon size={14}/></button>}
      </div>

      <p className="text-xs text-amber-800 mb-1">
        The repository does not compile at HEAD <strong>before</strong> any change. Fix the
        repo and re-run the baseline.
      </p>
      {suspendReason && (
        <pre className="text-[11px] text-amber-700 bg-amber-100/60 rounded p-2 mb-3 whitespace-pre-wrap break-words max-h-28 overflow-auto">{suspendReason}</pre>
      )}
      {!suspendReason && <div className="mb-3" />}

      {/* Skip tests opt-out — explicit user action only (PCI/DSS default = tests ON) */}
      <div className="border border-amber-200 rounded p-2 bg-amber-50/80 mb-3">
        <label className="flex items-center gap-2 cursor-pointer group">
          <input
            type="checkbox"
            checked={skipTests}
            onChange={e => setSkipTests(e.target.checked)}
            className="w-3.5 h-3.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-400"
          />
          <span className="text-xs text-gray-700 group-hover:text-gray-900">Skip Tests + SLT on resume</span>
          <span className="text-[10px] text-amber-600">(PCI/DSS: only waive if baseline issues prevent test creation)</span>
        </label>
      </div>

      <div className="flex flex-col gap-2">
        <button
          onClick={() => submit({})}
          disabled={loading !== null}
          className="w-full py-1.5 rounded text-xs font-semibold text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50 flex items-center justify-center gap-1">
          {loading === "self" ? <Loader2 size={12} className="animate-spin"/> : <RefreshCw size={12} />}
          I'll fix the repo — re-check baseline
        </button>
        <button
          onClick={() => submit({ skipCompile: true })}
          disabled={loading !== null}
          className="w-full py-1.5 rounded text-xs font-semibold text-orange-700 bg-orange-100 hover:bg-orange-200 border border-orange-300 disabled:opacity-50 flex items-center justify-center gap-1">
          {loading === "skip" ? <Loader2 size={12} className="animate-spin"/> : <AlertTriangle size={12} />}
          Skip compilation & continue
        </button>
      </div>

      <p className="text-[10px] text-amber-600 mt-2">
        Skipping compilation runs the pipeline without building — the code will be
        committed unverified.
      </p>
    </div>
  );
}

// ── Governance Scan suspended panel (governance end-gate resume) ────────
// A run SUSPENDED at GOVERNANCE_SCAN (blocking findings, or a scan error) must be
// resumed via the same resume-from-stage mechanism as StageActionPanel
// (POST /sdlc/runs/{id}/resume), but always with target_stage="GOVERNANCE_SCAN" —
// the backend worker re-runs governance, NOT implement, for that target. Kept as
// its own panel (mirroring BaselineActionPanel) so the primary action is
// unambiguous, rather than surfacing the generic retry/go_back/waive selector.
function GovernanceResumePanel({ runId, onClose, onDone }) {
  const { toast } = useToast();
  const [loading, setLoading] = useState(false);

  async function resumeGovernance() {
    setLoading(true);
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_stage: "GOVERNANCE_SCAN", mode: "retry" }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Governance resume failed");
      } else {
        toast.success("Governance scan resumed");
        onDone && onDone();
        onClose && onClose();
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="border border-violet-200 rounded-lg p-4 bg-violet-50 mt-2">
      <div className="flex items-center justify-between mb-2">
        <h4 className="font-semibold text-violet-900 text-sm flex items-center gap-1">
          <Shield size={14} /> Governance scan suspended
        </h4>
        {onClose && <button onClick={onClose} className="text-violet-400 hover:text-violet-600"><XIcon size={14}/></button>}
      </div>
      <p className="text-xs text-violet-800 mb-3">
        The pre-merge governance end-gate did not complete cleanly. Resume it to
        re-run the scan against the current diff.
      </p>
      <button
        onClick={resumeGovernance}
        disabled={loading}
        className="w-full py-1.5 rounded text-xs font-semibold text-white bg-violet-600 hover:bg-violet-700 disabled:opacity-50 flex items-center justify-center gap-1">
        {loading ? <Loader2 size={12} className="animate-spin"/> : <Shield size={12} />}
        {loading ? "Resuming…" : "Resume Governance Scan"}
      </button>
    </div>
  );
}
