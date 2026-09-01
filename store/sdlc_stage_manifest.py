# SPDX-License-Identifier: Apache-2.0
"""
store/sdlc_stage_manifest.py
────────────────────────────
Canonical, backend-owned SDLC pipeline stage manifest — the SINGLE SOURCE OF
TRUTH the UI renders its stage timeline from.

Why this module exists
-----------------------
The frontend historically carried THREE independently hand-maintained stage
models (`FEATURE_STAGES`/`BUG_STAGES` chips, `FEATURE_STAGE_ORDER`/`STATE_STYLE`,
and `STAGE_RENDERERS`) that drifted from each other and from the real backend
pipeline. This module replaces all of them: the UI fetches the manifest for a
run type and renders from it, so adding/removing a stage is a one-line change
here, not a 3-way frontend edit.

Design rules honored
---------------------
* Order + labels are derived from the REAL shift-left pipeline flow documented in
  CLAUDE.md ("decide before the gate") and reconciled with the backend state set
  (`store.sdlc_artifacts.STAGE_DAG` / `PRE_SM_STAGES_BY_TYPE`). The HITL gate sits
  AFTER pre-gate TESTING (a real compiled+tested VERIFIED_DIFF is approved), and
  APPLYING/TEST_VERIFY/SLT_RUNNING/COMMITTING/MR_CREATION are post-gate.
* Three-phase CLI hard cutover (2026-07-01): BOTH run types run one unconditional
  `PLAN` node (read-only CLI planner) → `IMPLEMENT` (one CLI session: code+tests+
  green) → `REVIEW` (one platform Opus pass over the diff). There is no planner
  mode; the feature and bug manifests differ only in the approval-gate label.
* `aliases` maps REMOVED/legacy states — the old planning heads (ANALYZING,
  DESIGNING, TROUBLESHOOTING, SOLUTIONING, DIAGNOSING, MANIFEST_VALIDATION) onto
  `PLAN`; codegen states (CODING, FIXING, TESTING, PRE_CODING_BUILD) onto
  `IMPLEMENT`; review states (REVIEWING, REVIEW_GATE, CROSS_MODEL_REVIEW) onto
  `REVIEW` — plus transient meta-states (`APPROVED`, `REVISION_REQUESTED`,
  `COMMIT_FAILED`, the PR-review loop, …) so HISTORICAL runs never render a blank
  timeline (audit requirement).
* `icon_key` is a STRING (lucide icon name); the UI maps it to a component. This
  module imports nothing UI-related and is safe to import without a DB session.

Public API
----------
pipeline_manifest(run_type) -> dict
    {"run_type", "planner_mode", "nodes": [...], "aliases": {...},
     "terminal_states": [...]}
    Each node: {id, label, group, kind, isGate, icon_key, optional, description}.

resolve_state(state, run_type) -> str | None
    Map a raw run state to a manifest node id (via direct match then aliases).
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from core.logger import logger
except Exception:  # pragma: no cover — allow isolated import outside the venv
    import logging
    logger = logging.getLogger(__name__)


# ── kind constants ────────────────────────────────────────────────────────────
KIND_STAGE = "stage"
KIND_GATE = "gate"
KIND_TERMINAL = "terminal"


# ── Terminal states (flagged kind:"terminal") ─────────────────────────────────
_TERMINAL_STATES: tuple = ("COMPLETE", "MERGED", "FAILED", "CANCELLED", "EXPIRED")


# Three-phase CLI hard cutover: the SDLC_PLANNER_MODE split/merged lever and the
# backend helper `_planner_merged_enabled` were REMOVED. Both run types now run one
# unconditional PLAN phase (read-only CLI planner), then IMPLEMENT (one CLI session:
# code + tests + drive-to-green), then REVIEW (one platform Opus pass over the diff).
# There is no planner mode any more.


# ── Node builders ──────────────────────────────────────────────────────────────

def _node(
    node_id: str,
    label: str,
    group: str,
    icon_key: str,
    *,
    kind: str = KIND_STAGE,
    is_gate: bool = False,
    optional: bool = False,
    description: str = "",
) -> dict:
    return {
        "id": node_id,
        "label": label,
        "group": group,
        "kind": kind,
        "isGate": is_gate,
        "icon_key": icon_key,
        "optional": optional,
        "description": description,
    }


# Nodes shared by feature + bug, BEFORE the planning/diagnosis divergence.
def _intake_nodes() -> list:
    return [
        _node("PREFLIGHT", "Preflight", "intake", "shield-check",
              description="Token / repo / Jira connectivity checks."),
        _node("BASELINE_BUILD", "Baseline Build", "intake", "hammer",
              description="Build HEAD as-is before any change (optional gate)."),
        _node("AWAITING_BUILD_METADATA_APPROVAL", "Confirm Build Version", "intake", "package-check",
              kind=KIND_GATE, is_gate=True, optional=True,
              description="Language version detected from the base branch differs from the "
                          "stored (product, repo) build metadata — human confirms which to use."),
        _node("TICKET_NORMALIZATION", "Normalise Ticket", "intake", "ticket",
              description="Extract a structured WorkItem from the raw ticket."),
        _node("AWAITING_USER_INPUT", "Clarify", "intake", "help-circle",
              kind=KIND_GATE, is_gate=True, optional=True,
              description="Paused for human answers to open questions."),
        _node("CLASSIFYING", "Classify", "intake", "file-text",
              description="Triage complexity + predict affected components."),
    ]


# Post-gate deterministic delivery tail (shared by feature + bug).
def _delivery_nodes() -> list:
    return [
        _node("APPLYING", "Apply", "delivery", "git-merge",
              description="Deterministic re-apply of the approved VERIFIED_DIFF."),
        _node("TEST_VERIFY", "Verify Tests", "delivery", "test-tube-2",
              description="Deterministic re-run of the unit tests on the applied tree."),
        _node("SLT_RUNNING", "SLT", "delivery", "shield", optional=True,
              description="System-level tests (skipped when skip_tests)."),
        _node("COMMITTING", "Commit", "delivery", "git-pull-request",
              description="Atomic commit to the working branch."),
        _node("MR_CREATION", "Create MR", "delivery", "git-pull-request",
              description="Open / update the GitLab merge request."),
    ]


def _terminal_nodes() -> list:
    return [
        _node("COMPLETE", "Complete", "terminal", "check-circle-2", kind=KIND_TERMINAL),
        _node("MERGED", "Merged", "terminal", "check-circle-2", kind=KIND_TERMINAL),
        _node("FAILED", "Failed", "terminal", "x-circle", kind=KIND_TERMINAL),
        _node("CANCELLED", "Cancelled", "terminal", "x-circle", kind=KIND_TERMINAL),
        _node("EXPIRED", "Expired", "terminal", "clock", kind=KIND_TERMINAL),
    ]


def _planning_node() -> list:
    """PLAN — the single read-only CLI planner phase (replaces ANALYZE+DESIGN and
    the bug DIAGNOSE/TROUBLESHOOT/SOLUTION heads). Emits the implementation plan +
    open_questions; the RETAINED grounding/convergence gate + manifest-validation
    sub-check run inside it."""
    return [
        _node("PLAN", "Plan", "planning", "book-open",
              description="Read-only planning — files to change, approach, tests, "
                          "grounding gate + clarify-in-plan."),
    ]


# Pre-gate IMPLEMENT + REVIEW (shared by feature + bug after the cutover).
def _implement_review_nodes(*, code_label: str) -> list:
    return [
        _node("IMPLEMENT", code_label, "implementation", "code-2",
              description="Writes code + tests and drives them to green "
                          "(compile/test oracle)."),
        _node("REVIEW", "Review Diff", "review", "git-branch",
              description="One platform Opus pass over the captured diff only "
                          "(one bounded fix round)."),
    ]


# ── Manifest assembly ───────────────────────────────────────────────────────────

def _feature_nodes() -> list:
    """Feature pipeline (and bugs — unified) — three-phase CLI engine: PLAN →
    IMPLEMENT → REVIEW run PRE-gate, the human approves a real VERIFIED_DIFF, and
    post-gate is deterministic apply+verify."""
    nodes: list = []
    nodes += _intake_nodes()
    nodes += _planning_node()
    nodes += _implement_review_nodes(code_label="Implement")
    # The relocated HITL gate — approves the real, compiled+tested VERIFIED_DIFF.
    nodes.append(_node("AWAITING_CODE_APPROVAL", "Approve Diff", "review", "thumbs-up",
                       kind=KIND_GATE, is_gate=True,
                       description="Human approves the verified diff (compiled+tested)."))
    nodes += _delivery_nodes()
    nodes += _governance_tail_nodes()
    nodes += _terminal_nodes()
    return nodes


def _bug_nodes() -> list:
    """Bug pipeline — UNIFIED into the same PLAN → IMPLEMENT → REVIEW three-phase
    flow as features (the diagnose/troubleshoot/solution heads were removed). Only
    the approval-gate node label differs (Approve Fix vs Approve Diff)."""
    nodes: list = []
    nodes += _intake_nodes()
    nodes += _planning_node()
    nodes += _implement_review_nodes(code_label="Fix Code")
    nodes.append(_node("AWAITING_SOLUTION_APPROVAL", "Approve Fix", "review", "thumbs-up",
                       kind=KIND_GATE, is_gate=True,
                       description="Human approves the verified fix diff (compiled+tested)."))
    nodes += _delivery_nodes()
    nodes += _governance_tail_nodes()
    nodes += _terminal_nodes()
    return nodes


def _governance_nodes() -> list:
    """Standalone governance pipeline (run_type="governance") — agentic per-skill
    scan → domain approval HITL gate → auto-fix → re-scan → commit → MR."""
    return [
        _node("GOVERNANCE_SCAN", "Scan", "scan", "shield",
              description="Agentic per-skill scan sessions execute each skill's analyzer over the diff."),
        _node("AWAITING_GOVERNANCE_APPROVAL", "Domain Approval", "approval", "users",
              kind=KIND_GATE, is_gate=True,
              description="Each governance domain (IS/EA/DPDP) must be triaged and approved by its "
                          "designated team before fixes proceed."),
        _node("GOVERNANCE_FIX", "Auto-Fix", "fix", "wrench",
              description="Auto-fixer applies fixes for all confirmed (non-false-positive) findings."),
        _node("GOVERNANCE_REVERIFY", "Re-scan", "fix", "shield-check",
              description="Re-scan the fixed diff to verify all findings are resolved."),
        _node("COMMITTING", "Commit", "delivery", "git-pull-request",
              description="Commit the governance fixes to the head branch."),
        _node("MR_CREATION", "Create MR", "delivery", "git-pull-request",
              description="Open / update the GitLab merge request (head branch → production)."),
        _node("COMPLETE", "Complete", "terminal", "check-circle-2", kind=KIND_TERMINAL),
        _node("FAILED", "Failed", "terminal", "x-circle", kind=KIND_TERMINAL),
        _node("CANCELLED", "Cancelled", "terminal", "x-circle", kind=KIND_TERMINAL),
        _node("EXPIRED", "Expired", "terminal", "clock", kind=KIND_TERMINAL),
    ]


# Governance tail (2026-07-24, re-ordered 2026-07-30) — appended AFTER MR_CREATION
# in the feature/bug manifests as an optional, waivable POST-merge-request
# governance pass. Mirrors the label/icon of the standalone governance pipeline's
# own scan/fix/reverify/approval nodes (see _governance_nodes() above), in the
# REAL runtime order: Scan -> Author Fixing -> Re-scan -> Domain Approval. The
# backend holds run.state at AWAITING_GOVERNANCE_APPROVAL through author-fix and
# re-scan, signalling the live sub-phase via run.context.governance_rescanning /
# run.context.governance_submitted_to_teams — the UI stepper re-points the active
# node using those flags (see PipelineStepper.jsx computeNodeStatuses).
def _governance_tail_nodes() -> list:
    return [
        _node("GOVERNANCE_SCAN", "Scan", "scan", "shield", optional=True,
              description="Agentic per-skill scan sessions execute each skill's analyzer over the diff."),
        _node("GOVERNANCE_FIX", "Author Fixing", "fix", "wrench",
              description="Run owner triages findings and the auto-fixer applies requested fixes."),
        _node("GOVERNANCE_REVERIFY", "Re-scan", "fix", "shield-check",
              description="Re-scan the fixed diff to verify the findings are resolved."),
        _node("AWAITING_GOVERNANCE_APPROVAL", "Domain Approval", "approval", "users",
              kind=KIND_GATE, is_gate=True,
              description="Each governance domain (IS/EA/DPDP) must be triaged and approved by its "
                          "designated team before fixes proceed."),
    ]


def _common_aliases() -> dict:
    """Legacy/removed + transient meta states → a renderable manifest node id.

    Keeps historical runs (whose stored events still carry removed stages) and
    in-flight meta states from blanking the timeline. Values MUST be a node id
    present in the manifest for the run type.
    """
    return {
        "CREATED": "PREFLIGHT",
        "TRIAGING": "CLASSIFYING",
        # ── Three-phase CLI cutover: every removed planning/codegen stage folds onto
        #    one of the three live nodes (PLAN / IMPLEMENT / REVIEW) so HISTORICAL runs
        #    (whose stored events still carry the old stages) never blank the timeline.
        # Planning heads → PLAN.
        "ANALYZING": "PLAN", "ANALYZE": "PLAN",
        "DESIGNING": "PLAN", "DESIGN": "PLAN",
        "TROUBLESHOOTING": "PLAN", "SOLUTIONING": "PLAN", "DIAGNOSING": "PLAN",
        "MANIFEST_VALIDATION": "PLAN",   # now a sub-check inside PLAN
        "REVISION_REQUESTED": "PLAN",
        # Renamed 2026-07-29: legacy code-approval gate state → the renamed node so
        # in-flight rows written before the rename still light the stepper.
        "AWAITING_DESIGN_APPROVAL": "AWAITING_CODE_APPROVAL",
        # Codegen/compile/test → IMPLEMENT.
        "PRE_CODING_BUILD": "IMPLEMENT",
        "CODING": "IMPLEMENT",
        "FIXING": "IMPLEMENT",
        "TESTING": "IMPLEMENT",
        "SLT": "IMPLEMENT",
        # Review heads → REVIEW.
        "REVIEWING": "REVIEW",
        "REVIEW_GATE": "REVIEW",
        "CROSS_MODEL_REVIEW": "REVIEW",
        # Meta / transient states.
        "APPROVED": "APPLYING",
        "MERGE_READY": "MR_CREATION",
        "COMMIT_FAILED": "COMMITTING",
        "WAIVED": "IMPLEMENT",
        # PR-review loop (post-MR) — fold onto MR_CREATION so the timeline stays bounded.
        "AWAITING_PR_APPROVAL": "MR_CREATION",
        "PR_REVIEW_COMMENTS_RECEIVED": "MR_CREATION",
        "AI_ADDRESSING_COMMENTS": "MR_CREATION",
        "AWAITING_RE_REVIEW": "MR_CREATION",
        "MERGE_CONFLICT": "MR_CREATION",
        # Governance tail (2026-07-24) — legacy inline GOVERNANCE_REVIEW state folds
        # onto the scan node. GOVERNANCE_FIX / GOVERNANCE_REVERIFY are now their own
        # live nodes in the feature/bug manifest (2026-07-30 re-order), so they are
        # NOT aliased onto GOVERNANCE_SCAN any more.
        "GOVERNANCE_REVIEW": "GOVERNANCE_SCAN",
    }


def pipeline_manifest(run_type: str) -> dict:
    """Return the canonical stage manifest for a run type.

    Parameters
    ----------
    run_type : str
        "feature" | "bug" | "governance" (anything else → feature shape + a WARN).

    Returns
    -------
    dict with keys:
        run_type        — normalized run type the manifest describes
        planner_mode    — "merged" | "split" (feature only; "n/a" for bug)
        nodes           — ordered list of node dicts (see module docstring)
        aliases         — {raw_state: node_id} for legacy/transient states
        terminal_states — list of terminal state ids
    """
    try:
        rt = (run_type or "feature").strip().lower()

        if rt == "bug":
            nodes = _bug_nodes()
            aliases = dict(_common_aliases())
            planner_mode = "n/a"
        elif rt == "governance":
            nodes = _governance_nodes()
            aliases = {
                # HITL gate state recognised by the UI stepper.
                "AWAITING_GOVERNANCE_APPROVAL": "AWAITING_GOVERNANCE_APPROVAL",
                # Common terminal / meta states.
                "CANCELLED": "CANCELLED",
                "EXPIRED":   "EXPIRED",
            }
            planner_mode = "n/a"
        else:
            if rt not in ("feature", "pr_review"):
                logger.warning(
                    "[SDLC-MANIFEST] unknown run_type requested — defaulting to feature",
                    run_type=run_type,
                )
                rt = "feature"
            nodes = _feature_nodes()
            aliases = dict(_common_aliases())
            # Three-phase CLI engine: one unconditional PLAN phase, no planner mode.
            planner_mode = "plan"

        # Defensive invariant: every alias MUST target a node id that exists in
        # THIS run type's manifest — a dangling alias would make resolve_state /
        # the UI point at a node that isn't rendered. Drop any that don't (the
        # named legacy states are all remapped above to valid targets).
        _node_ids = {n["id"] for n in nodes}
        aliases = {k: v for k, v in aliases.items() if v in _node_ids}

        return {
            "run_type": rt,
            "planner_mode": planner_mode,
            "nodes": nodes,
            "aliases": aliases,
            "terminal_states": list(_TERMINAL_STATES),
        }
    except Exception:
        logger.error(
            "[SDLC-MANIFEST] manifest build failure",
            run_type=run_type,
        )
        raise


def resolve_state(state: str, run_type: str = "feature") -> Optional[str]:
    """Map a raw run state to a manifest node id (direct match, then aliases).

    Returns None when the state cannot be mapped — callers should treat that as
    "render via fallback", never blank.
    """
    if not state:
        return None
    s = state.strip().upper()
    manifest = pipeline_manifest(run_type)
    node_ids = {n["id"] for n in manifest["nodes"]}
    if s in node_ids:
        return s
    return manifest["aliases"].get(s)
