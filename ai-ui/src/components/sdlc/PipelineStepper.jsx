// SPDX-License-Identifier: MIT
// ============================================================
// PipelineStepper.jsx — backend-manifest-driven stage timeline
// ------------------------------------------------------------
// REPLACES the three hand-maintained stage models that used to
// live in SDLCPipeline.jsx (FEATURE_STAGES / BUG_STAGES + the
// FEATURE_STAGE_ORDER / BUG_STAGE_ORDER + stageIndex() mapping).
//
// It fetches the canonical pipeline manifest for a run type from
//   GET {API}/sdlc/pipeline-manifest?type=<feature|bug>
// (nodes already in pipeline order), then maps each manifest node
// to a LIVE per-node status by comparing the manifest against the
// run's current state + event history, and renders a horizontal /
// wrapping timeline with layered status + specific microcopy.
//
// All colours / icons / microcopy come from the shared statusModel
// — this component derives NOTHING about styling locally.
// ============================================================

import { createElement, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

import { API_BASE as API, apiFetch } from "../../config";
import {
  iconFor,
  nodeStatusStyle,
  nodeMicrocopy,
  isTerminal,
  isGateState,
  TERMINAL_STATES,
} from "./statusModel.js";

// Module-level manifest cache, keyed by run type ("feature" | "bug").
// Survives re-renders AND remounts so we never refetch on every poll.
const _MANIFEST_CACHE = {};

// "done" if the terminal state is a success, "failed" otherwise.
const _SUCCESS_TERMINALS = new Set(["COMPLETE", "MERGED"]);

// Opt-in stages that only fire when an explicit trigger flag is set.
// When such a stage was never reached and there is no evidence that the
// opt-in ran, we render it as "skipped" rather than "pending" so the
// stepper does not show a false "stuck" step for governance-disabled runs.
// GOVERNANCE_SCAN fires only when run_governance_review=true; GOVERNANCE_FIX /
// GOVERNANCE_REVERIFY are the same opt-in governance tail's fix/re-scan sub-phases.
const OPTIONAL_STAGES = new Set(["GOVERNANCE_SCAN", "GOVERNANCE_FIX", "GOVERNANCE_REVERIFY"]);

// ── Live state → node id resolution ───────────────────────────
// A raw run/event state resolves to a node id by:
//   1. direct match against a node id
//   2. else aliases[rawState]
//   3. else null (unmapped — caller must stay defensive)
function resolveNodeId(rawState, nodeIdSet, aliases) {
  if (!rawState) return null;
  if (nodeIdSet.has(rawState)) return rawState;
  const aliased = aliases?.[rawState];
  if (aliased && nodeIdSet.has(aliased)) return aliased;
  return null;
}

// ── The critical mapping ──────────────────────────────────────
// Pure function: given the ordered manifest nodes, the alias table,
// the run, and its events, return the nodes annotated with a
// `nodeStatus` ∈ "done"|"active"|"gate"|"failed"|"pending"|"skipped".
// NEVER throws; unmapped states degrade gracefully (no active highlight).
// Module-internal (not exported) so this file only exports the component
// — keeps react-refresh happy and the public surface = <PipelineStepper>.
function computeNodeStatuses(nodes, aliases, run, events) {
  const safeNodes = Array.isArray(nodes) ? nodes : [];
  const safeAliases = aliases || {};
  const safeEvents = Array.isArray(events) ? events : [];
  const state = run?.state;

  // Fast index lookups by node id.
  const nodeIdSet = new Set(safeNodes.map((n) => n?.id));
  const indexById = new Map(safeNodes.map((n, i) => [n?.id, i]));

  // 1. Resolve the ACTIVE node id from the live run state.
  let resolvedActiveId = resolveNodeId(state, nodeIdSet, safeAliases);
  // Governance sub-phase: backend holds run.state at AWAITING_GOVERNANCE_APPROVAL
  // through author-fix + re-scan, signalling the live sub-phase via context flags.
  // Re-point the active node so the stepper shows Fixing → Re-scan → Approval in order.
  if (state === "AWAITING_GOVERNANCE_APPROVAL") {
    // Read flags from run.context first, then the flattened run.* (mirrors the
    // panel's getRunFlag resolution) — the run serializer may expose either shape.
    const ctx = run?.context || {};
    const _flag = (k) => Boolean(ctx[k] !== undefined ? ctx[k] : run?.[k]);
    const rescanning = _flag("governance_rescanning");
    const submitted = _flag("governance_submitted_to_teams");
    if (rescanning && nodeIdSet.has("GOVERNANCE_REVERIFY")) {
      resolvedActiveId = "GOVERNANCE_REVERIFY";
    } else if (!submitted && nodeIdSet.has("GOVERNANCE_FIX")) {
      resolvedActiveId = "GOVERNANCE_FIX";
    } // else: submitted → keep AWAITING_GOVERNANCE_APPROVAL (the gate)
  }
  const activeId = resolvedActiveId;
  const activeIdx = activeId != null ? indexById.get(activeId) : undefined;

  // 2. Collect every node id REACHED from history: event.to_state,
  //    event.stage, and the explicit run.current_stage — each resolved
  //    through (direct id → alias). This lets a node that was visited
  //    but is now behind the active node still render "done", and lets
  //    us detect which optional nodes were skipped.
  const reached = new Set();
  let maxReachedIdx = -1;
  const noteReached = (raw) => {
    const id = resolveNodeId(raw, nodeIdSet, safeAliases);
    if (id != null) {
      reached.add(id);
      const idx = indexById.get(id);
      if (idx != null && idx > maxReachedIdx) maxReachedIdx = idx;
    }
  };
  for (const ev of safeEvents) {
    noteReached(ev?.to_state);
    noteReached(ev?.stage);
  }
  noteReached(run?.current_stage);
  if (activeIdx != null && activeIdx > maxReachedIdx) maxReachedIdx = activeIdx;

  // Governance-ran signal: true if GOVERNANCE_SCAN appeared in event history
  // OR the run is currently waiting at the governance approval gate.
  // When false, GOVERNANCE_SCAN is treated as effectively-optional (skipped),
  // not pending — governance was not opted-in at trigger time.
  const governanceRan =
    reached.has("GOVERNANCE_SCAN") ||
    run?.state === "AWAITING_GOVERNANCE_APPROVAL";

  // Unifies manifest-level optional nodes with client-side OPTIONAL_STAGES:
  // a node is effectively optional if the manifest says so OR if it belongs
  // to an opt-in stage whose opt-in did not fire for this run.
  const isEffectivelyOptional = (node) =>
    node?.optional || (OPTIONAL_STAGES.has(node?.id) && !governanceRan);

  const terminal = isTerminal(state);

  return safeNodes.map((node, idx) => {
    const isGateNode = node?.isGate || isGateState(node?.id);

    // ── Terminal run: collapse the timeline. ──────────────────
    if (terminal) {
      // Only the terminal node that actually matches the run state is
      // highlighted; sibling terminal nodes (e.g. FAILED node on a
      // COMPLETE run) stay pending so the tail reads cleanly.
      if (node?.kind === "terminal" || TERMINAL_STATES.has(node?.id)) {
        if (node?.id === state) {
          return { ...node, nodeStatus: _SUCCESS_TERMINALS.has(state) ? "done" : "failed" };
        }
        return { ...node, nodeStatus: "pending" };
      }
      // Non-terminal nodes: everything up to & including the last reached
      // node reads "done"; anything genuinely never reached stays pending
      // (covers FAILED runs that bailed mid-pipeline).
      if (idx <= maxReachedIdx) {
        if (isEffectivelyOptional(node) && !reached.has(node?.id)) return { ...node, nodeStatus: "skipped" };
        return { ...node, nodeStatus: "done" };
      }
      // Past maxReachedIdx — opt-in stages that never fired should be skipped, not pending.
      if (isEffectivelyOptional(node) && !reached.has(node?.id)) return { ...node, nodeStatus: "skipped" };
      return { ...node, nodeStatus: "pending" };
    }

    // ── In-flight run with a resolved active node. ────────────
    if (activeIdx != null) {
      if (idx === activeIdx) {
        return { ...node, nodeStatus: isGateNode ? "gate" : "active" };
      }
      if (idx < activeIdx) {
        // Behind the active node. An optional node never reached → skipped.
        if (isEffectivelyOptional(node) && !reached.has(node?.id)) return { ...node, nodeStatus: "skipped" };
        return { ...node, nodeStatus: "done" };
      }
      // Ahead of the active node — but a node already reached in events
      // (e.g. a loop revisited a later node) still shows done.
      if (reached.has(node?.id)) return { ...node, nodeStatus: "done" };
      // Opt-in stages that didn't fire: show skipped rather than pending.
      if (isEffectivelyOptional(node)) return { ...node, nodeStatus: "skipped" };
      return { ...node, nodeStatus: "pending" };
    }

    // ── Defensive fallback: active state unmapped. ────────────
    // Best-effort using only history; no active highlight, never blank.
    if (idx <= maxReachedIdx) {
      if (isEffectivelyOptional(node) && !reached.has(node?.id)) return { ...node, nodeStatus: "skipped" };
      return { ...node, nodeStatus: "done" };
    }
    // Past maxReachedIdx — opt-in stages that never fired should be skipped.
    if (isEffectivelyOptional(node)) return { ...node, nodeStatus: "skipped" };
    return { ...node, nodeStatus: "pending" };
  });
}

// Planning nodes that get convergence-aware microcopy.
// Three-phase CLI engine collapse (2026-07-01): ANALYZING/DESIGNING/
// TROUBLESHOOTING/SOLUTIONING/DIAGNOSING no longer exist as live stages —
// PLAN is the single planning node for both feature and bug runs.
const _PLANNING_IDS = new Set(["PLAN"]);

// ── A single node chip ────────────────────────────────────────
function StepNode({ node, convergence, isLast, onNodeClick }) {
  const style = nodeStatusStyle(node.nodeStatus);
  const Icon = iconFor(node.icon_key);
  const isActive = node.nodeStatus === "active" || node.nodeStatus === "gate";
  const showMicrocopy = isActive;
  const microcopy = showMicrocopy
    ? nodeMicrocopy(node, _PLANNING_IDS.has(node.id) ? { convergence } : {})
    : "";
  // Click-to-open-artifact: only stages/gates are inspectable, never terminals.
  const clickable = typeof onNodeClick === "function" && node.kind !== "terminal";

  return (
    <div className="flex items-start">
      <div
        className={`flex flex-col items-center text-center w-14 ${clickable ? "cursor-pointer" : ""}`}
        onClick={clickable ? () => onNodeClick(node) : undefined}
        title={clickable ? `View ${node.label} artifact` : (node.description || node.label)}
      >
        {/* icon disc — colour + ring encode status (gate = yellow) */}
        <div
          className={`flex items-center justify-center w-6 h-6 rounded-full text-white ${style.dot} ${style.ring}`}
          title={node.description || node.label}
        >
          {/* icon component is data-driven (from iconFor) — render via
              createElement so it is not treated as a render-local component */}
          {createElement(Icon, { size: 11 })}
        </div>
        <span className={`mt-0.5 text-[9px] font-medium leading-tight ${style.text}`}>
          {node.label}
        </span>
        {microcopy && (
          <span className="mt-0.5 text-[8px] text-gray-400 leading-tight">{microcopy}</span>
        )}
      </div>
      {/* connector line to the next node (skipped for the last one) */}
      {!isLast && (
        <div
          className={`h-px w-3 mt-3 flex-shrink-0 ${
            node.nodeStatus === "done" ? "bg-green-300" : "bg-gray-200"
          }`}
        />
      )}
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────
export default function PipelineStepper({ run, events, convergence, onNodeClick }) {
  const type = run?.type === "bug" ? "bug"
             : run?.type === "governance" ? "governance"
             : run?.type === "pr_review" ? "pr_review"
             : "feature";
  const [manifest, setManifest] = useState(() => _MANIFEST_CACHE[type] || null);
  const [loading, setLoading] = useState(!_MANIFEST_CACHE[type]);
  const [error, setError] = useState(false);
  // Guard against setState after unmount on a slow fetch.
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    // Cache hit → no fetch.
    if (_MANIFEST_CACHE[type]) {
      setManifest(_MANIFEST_CACHE[type]);
      setLoading(false);
      setError(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(false);
    (async () => {
      try {
        const resp = await apiFetch(`${API}/sdlc/pipeline-manifest?type=${type}`);
        if (!resp.ok) throw new Error(`manifest ${resp.status}`);
        const data = await resp.json();
        if (cancelled || !mountedRef.current) return;
        _MANIFEST_CACHE[type] = data;
        setManifest(data);
      } catch {
        if (cancelled || !mountedRef.current) return;
        setError(true);
      } finally {
        if (!cancelled && mountedRef.current) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [type]);

  // Loading skeleton.
  if (loading) {
    return (
      <div className="flex items-center gap-2 py-3 text-xs text-gray-400">
        <Loader2 size={12} className="animate-spin" /> Loading pipeline stages…
      </div>
    );
  }

  // Error / empty manifest → minimal line, never crash.
  const nodes = manifest?.nodes;
  if (error || !Array.isArray(nodes) || nodes.length === 0) {
    return <div className="py-2 text-xs text-gray-400">Pipeline stages unavailable.</div>;
  }

  const evs = Array.isArray(events) ? events : run?.events || [];
  const computed = computeNodeStatuses(nodes, manifest?.aliases, run, evs);

  return (
    <div className="flex flex-row flex-wrap gap-y-3 py-2">
      {computed.map((node, i) => (
        <StepNode
          key={node.id || i}
          node={node}
          convergence={convergence}
          isLast={i === computed.length - 1}
          onNodeClick={onNodeClick}
        />
      ))}
    </div>
  );
}
