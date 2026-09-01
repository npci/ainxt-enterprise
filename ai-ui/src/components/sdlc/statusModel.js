// SPDX-License-Identifier: Apache-2.0
// ============================================================
// statusModel.js — Shared SDLC status/style/icon model
// ------------------------------------------------------------
// Single home for run-state colours + labels, manifest icon_key →
// lucide-icon mapping, and microcopy helpers. This DEDUPES the old
// hand-maintained STATE_STYLE map that used to live in SDLCPipeline.jsx
// and is imported by every SDLC panel (PipelineStepper, ConvergencePanel,
// RunMetricsPanel, the gate, …) so there is exactly ONE state map.
//
// Status keys here align with the backend manifest `kind`/state set
// (store/sdlc_stage_manifest.py) and the legacy/transient states still
// present on historical runs.
// ============================================================

import {
  ShieldCheck, Hammer, Ticket, HelpCircle, FileText, BookOpen, Code2,
  ClipboardCheck, ThumbsUp, GitBranch, TestTube2, GitMerge, Shield,
  GitPullRequest, CheckCircle2, XCircle, Clock, Search, Circle, Wrench,
  PackageCheck,
} from "lucide-react";

// ── State → {color, label} ────────────────────────────────────
// Migrated verbatim from SDLCPipeline.jsx STATE_STYLE (the last
// hand-maintained state map). Add new states HERE only.
export const STATUS_STYLE = {
  CREATED:                    { color: "bg-gray-200 text-gray-700",   label: "Created" },
  BASELINE_BUILD:             { color: "bg-amber-100 text-amber-800 ring-2 ring-amber-400", label: "🔨 Baseline build" },
  CLASSIFYING:                { color: "bg-blue-100 text-blue-700",   label: "Classifying" },
  ANALYZING:                  { color: "bg-blue-100 text-blue-700",   label: "Analysing" },
  TRIAGING:                   { color: "bg-blue-100 text-blue-700",   label: "Triaging" },
  TROUBLESHOOTING:            { color: "bg-blue-100 text-blue-700",   label: "Troubleshooting" },
  DESIGNING:                  { color: "bg-indigo-100 text-indigo-700", label: "Designing" },
  SOLUTIONING:                { color: "bg-indigo-100 text-indigo-700", label: "Fix Planning" },
  PLAN:                       { color: "bg-indigo-100 text-indigo-700", label: "Planning" },
  PLANNING:                   { color: "bg-indigo-100 text-indigo-700", label: "Planning" },
  // Three-phase CLI engine (2026-07-01 cutover): IMPLEMENT (one CLI session,
  // code+tests+green) → REVIEW (platform Opus diff-only gate) → VERIFIED_DIFF
  // (the artifact the HITL gate approves).
  IMPLEMENT:                  { color: "bg-purple-100 text-purple-700", label: "💻 Implementing" },
  REVIEW:                     { color: "bg-purple-100 text-purple-700", label: "🔍 Reviewing Diff" },
  VERIFIED_DIFF:              { color: "bg-blue-100 text-blue-700",     label: "Verified Diff" },
  // AWAITING_CODE_APPROVAL is the renamed replacement for AWAITING_DESIGN_APPROVAL
  // (2026-07-29). Old rows still carry the legacy value, so both keys share the
  // exact same visuals/label object — the UI must render either identically.
  AWAITING_CODE_APPROVAL:     { color: "bg-yellow-100 text-yellow-800", label: "⏳ Code Approval" },
  AWAITING_DESIGN_APPROVAL:   { color: "bg-yellow-100 text-yellow-800", label: "⏳ Code Approval" },
  AWAITING_SOLUTION_APPROVAL: { color: "bg-yellow-100 text-yellow-800", label: "⏳ Needs Approval" },
  AWAITING_USER_INPUT:        { color: "bg-yellow-100 text-yellow-800", label: "Needs Input" },
  AWAITING_BUILD_METADATA_APPROVAL: { color: "bg-yellow-100 text-yellow-800", label: "⏳ Confirm Build Version" },
  REVISION_REQUESTED:         { color: "bg-amber-100 text-amber-700",  label: "↩ Revision Requested" },
  CODING:                     { color: "bg-purple-100 text-purple-700", label: "Coding" },
  REVIEWING:                  { color: "bg-purple-100 text-purple-700", label: "Reviewing" },
  REVIEW_GATE:                { color: "bg-purple-100 text-purple-700", label: "Review Gate" },
  FIXING:                     { color: "bg-orange-100 text-orange-700", label: "↺ Fixing (inline)" },
  TESTING:                    { color: "bg-cyan-100 text-cyan-700",    label: "Testing" },
  APPLYING:                   { color: "bg-purple-100 text-purple-700", label: "Applying" },
  TEST_VERIFY:                { color: "bg-cyan-100 text-cyan-700",    label: "Verifying Tests" },
  SLT_RUNNING:                { color: "bg-cyan-100 text-cyan-700",    label: "SLT Running" },
  COMMITTING:                 { color: "bg-teal-100 text-teal-700",   label: "Committing" },
  COMMIT_FAILED:              { color: "bg-orange-100 text-orange-700 ring-2 ring-orange-400", label: "↺ Commit Failed (Resumable)" },
  AWAITING_PR_APPROVAL:           { color: "bg-yellow-100 text-yellow-800", label: "⏳ PR Approval" },
  PR_REVIEW_COMMENTS_RECEIVED:    { color: "bg-orange-100 text-orange-700", label: "Review Comments" },
  AI_ADDRESSING_COMMENTS:         { color: "bg-purple-100 text-purple-700", label: "AI Addressing Comments" },
  AWAITING_RE_REVIEW:             { color: "bg-yellow-100 text-yellow-800", label: "⏳ Re-Review" },
  MERGE_CONFLICT:                 { color: "bg-red-100 text-red-700",       label: "⚠ Merge Conflict" },
  MERGE_READY:                    { color: "bg-teal-100 text-teal-700",     label: "Merge Ready" },
  MERGED:                         { color: "bg-green-100 text-green-700",   label: "✓ Merged" },
  COMPLETE:                       { color: "bg-green-100 text-green-700",   label: "✓ Complete" },
  FAILED:                         { color: "bg-red-100 text-red-700",       label: "✗ Failed" },
  CANCELLED:                      { color: "bg-gray-300 text-gray-600",     label: "✗ Cancelled" },
  EXPIRED:                        { color: "bg-gray-300 text-gray-600",     label: "⌛ Expired" },
  APPROVED:                       { color: "bg-blue-100 text-blue-700",     label: "Approved" },
  SUSPENDED:                      { color: "bg-orange-100 text-orange-700", label: "⏸ Suspended" },
  STALE:                          { color: "bg-gray-100 text-gray-400 line-through", label: "Stale" },
  WAIVED:                         { color: "bg-amber-100 text-amber-700 ring-2 ring-amber-400", label: "✓ Waived" },
  TICKET_NORMALIZATION:           { color: "bg-sky-100 text-sky-700",       label: "🎫 Normalising Ticket" },
  DIAGNOSING:                     { color: "bg-blue-100 text-blue-700",     label: "🔍 Diagnosing" },
  MANIFEST_VALIDATION:            { color: "bg-violet-100 text-violet-700", label: "📋 Validating Manifest" },
  PRE_CODING_BUILD:               { color: "bg-amber-100 text-amber-800",   label: "🔨 Pre-Code Build" },
  MR_CREATION:                    { color: "bg-teal-100 text-teal-700",     label: "🔀 Creating MR" },
  PREFLIGHT:                      { color: "bg-gray-200 text-gray-700",     label: "Preflight" },
  // Governance pipeline states (standalone run_type="governance" + in-pipeline gate).
  GOVERNANCE_SCAN:              { color: "bg-violet-100 text-violet-700",                        label: "🛡 Scanning" },
  GOVERNANCE_REVIEW:            { color: "bg-violet-100 text-violet-700",                        label: "🛡 Governance Review" },
  AWAITING_GOVERNANCE_APPROVAL: { color: "bg-yellow-100 text-yellow-800",                        label: "⏳ Domain Approval" },
  GOVERNANCE_FIX:               { color: "bg-orange-100 text-orange-700",                        label: "🔧 Governance Fix" },
  GOVERNANCE_REVERIFY:          { color: "bg-violet-100 text-violet-700",                        label: "🛡 Re-scanning" },
};

const _FALLBACK = { color: "bg-gray-100 text-gray-600", label: "" };

/** {color,label} for a run state — never throws; unknown → label = the raw state. */
export function statusStyle(state) {
  if (!state) return _FALLBACK;
  return STATUS_STYLE[state] || { ..._FALLBACK, label: state };
}

export function statusLabel(state) {
  return statusStyle(state).label || state || "—";
}

export function statusBadgeClass(state) {
  return statusStyle(state).color;
}

// Terminal / gate / attention classifiers (single source — mirror backend kinds).
export const TERMINAL_STATES = new Set(["COMPLETE", "MERGED", "FAILED", "CANCELLED", "EXPIRED"]);
export const GATE_STATES = new Set([
  "AWAITING_CODE_APPROVAL", "AWAITING_DESIGN_APPROVAL", "AWAITING_SOLUTION_APPROVAL", "AWAITING_USER_INPUT",
  "AWAITING_PR_APPROVAL", "AWAITING_RE_REVIEW", "MERGE_CONFLICT",
  "AWAITING_GOVERNANCE_APPROVAL", "AWAITING_BUILD_METADATA_APPROVAL",
]);
// States that need human attention (raised-hand affordance).
export const ATTENTION_STATES = new Set([...GATE_STATES, "COMMIT_FAILED", "SUSPENDED"]);

export function isTerminal(state) { return TERMINAL_STATES.has(state); }
export function isGateState(state) { return GATE_STATES.has(state); }
export function needsAttention(state) { return ATTENTION_STATES.has(state); }

// ── Manifest icon_key → lucide component ──────────────────────
// The backend manifest carries icon_key as a STRING; the UI maps it here.
const ICON_BY_KEY = {
  "shield-check": ShieldCheck,
  hammer: Hammer,
  ticket: Ticket,
  "help-circle": HelpCircle,
  "file-text": FileText,
  "book-open": BookOpen,
  "code-2": Code2,
  "clipboard-check": ClipboardCheck,
  "thumbs-up": ThumbsUp,
  "git-branch": GitBranch,
  "test-tube-2": TestTube2,
  "git-merge": GitMerge,
  shield: Shield,
  wrench: Wrench,
  "git-pull-request": GitPullRequest,
  "check-circle-2": CheckCircle2,
  "x-circle": XCircle,
  clock: Clock,
  search: Search,
  "package-check": PackageCheck,
};

/** Resolve a manifest icon_key to a lucide component; unknown → Circle. */
export function iconFor(iconKey) {
  return ICON_BY_KEY[iconKey] || Circle;
}

// ── Per-node live status (used by PipelineStepper) ────────────
// "done" | "active" | "gate" | "failed" | "pending" | "skipped"
export const NODE_STATUS_STYLE = {
  done:    { dot: "bg-green-500",  text: "text-green-700",  ring: "" },
  active:  { dot: "bg-blue-500 animate-pulse",   text: "text-blue-700",   ring: "ring-2 ring-blue-300" },
  gate:    { dot: "bg-yellow-500 animate-pulse", text: "text-yellow-800", ring: "ring-2 ring-yellow-400" },
  failed:  { dot: "bg-red-500",    text: "text-red-700",    ring: "ring-2 ring-red-300" },
  pending: { dot: "bg-gray-300",   text: "text-gray-400",   ring: "" },
  skipped: { dot: "bg-gray-200",   text: "text-gray-400 line-through", ring: "" },
};

export function nodeStatusStyle(nodeStatus) {
  return NODE_STATUS_STYLE[nodeStatus] || NODE_STATUS_STYLE.pending;
}

// ── Microcopy ─────────────────────────────────────────────────
// Specific, data-driven status text — preferred over generic spinners.
// `convergence` is the optional shape produced by ConvergencePanel:
//   { round, cap, gapsLeft }
export function nodeMicrocopy(node, { convergence } = {}) {
  if (!node) return "";
  const id = node.id;
  if ((id === "PLAN" || id === "ANALYZE" || id === "DESIGN" || id === "DIAGNOSING") && convergence) {
    const parts = [];
    if (convergence.round != null) {
      parts.push(convergence.cap != null
        ? `round ${convergence.round}/${convergence.cap}`
        : `round ${convergence.round}`);
    }
    if (convergence.gapsLeft != null && convergence.gapsLeft > 0) {
      parts.push(`${convergence.gapsLeft} gap${convergence.gapsLeft === 1 ? "" : "s"} left`);
    }
    if (parts.length) return `${node.label} · ${parts.join(" · ")}`;
  }
  return node.description || node.label || "";
}
