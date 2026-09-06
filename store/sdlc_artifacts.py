# SPDX-License-Identifier: MIT
# ============================================================
# SDLC STAGE ARTIFACTS STORE
# Versioned per-stage artifact persistence for the SDLC
# flexible pipeline.  Each pipeline stage inserts one row
# per attempt; re-runs increment version so diffs and
# cross-model comparisons are preserved.
# ============================================================

import hashlib
import json
import logging
import re

from sqlalchemy import text

# Use the platform logger so artifact-store failures are actually surfaced.
# A bare logging.getLogger(__name__) goes to an unconfigured logger and the
# warnings never appear in the worker/gateway logs — masking real DB errors.
try:
    from core.logger import logger
except Exception:  # pragma: no cover - fallback if core.logger unavailable
    logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage DAG — defines which upstream stages feed each stage.
# Keys are stage names; values are lists of direct upstream stage names.
# CLASSIFYING has no upstreams (it is the root stage).
# ---------------------------------------------------------------------------
# Three-phase CLI engine (hard cutover): PLAN → IMPLEMENT → REVIEW. The legacy
# ANALYZING/DESIGNING (feature) and their bug analogues, plus the intra-SM stages
# CODING/SLT/REVIEWING/CROSS_MODEL_REVIEW/FIXING/TESTING, are NO LONGER produced as
# resumable STAGE_DAG stages — codegen+compile+test are collapsed into IMPLEMENT and
# the Opus diff-review into REVIEW. PLAN is the sole pre-gate planning stage for both
# run types (bugs unified into PLAN).
STAGE_DAG: dict = {
    "CLASSIFYING":        [],
    "PLAN":               ["CLASSIFYING"],
    # MANIFEST_VALIDATION (gate-reorder, 2026-07-02) — produced INSIDE _run_plan_phase
    # (not a separate resumable stage in the pre-SM sequence), but stores its own
    # artifact for display/audit (WS-3/WS-4/P5). Structural check always runs; the
    # OpenAI cross-check is skipped for complexity=="simple".
    "MANIFEST_VALIDATION": ["PLAN"],
    "IMPLEMENT":          ["PLAN"],
    "REVIEW":             ["IMPLEMENT"],
    # GOVERNANCE_REVIEW (2026-07-17) — a SEPARATE governance gate (EA/IS/DPDP) that
    # runs AFTER the diff-only Opus REVIEW, over the same diff, via CLI plugins. It is
    # opt-in/waivable (NOT in MANDATORY_STAGES) and flag-gated: when governance is off
    # it simply produces no artifact and the pipeline flows REVIEW → VERIFIED_DIFF/
    # COMMITTING exactly as before (a missing upstream contributes "" to the input
    # hash — harmless for these free-form stores).
    # GOVERNANCE_REVIEW (LEGACY — end-gate overhaul 2026-07-23): governance NO LONGER
    # runs mid-pipeline over the pre-apply diff. It moved to an END-GATE that fires
    # AFTER COMMITTING + a draft MR (see GOVERNANCE_SCAN tail in SHARED_SM_STAGES and
    # agents/sdlc_state_machine.py::_run_governance_endgate). This edge is retained
    # ONLY so any historical run that already produced a GOVERNANCE_REVIEW artifact
    # still hashes coherently; nothing downstream depends on it anymore.
    "GOVERNANCE_REVIEW":  ["REVIEW"],
    # GOVERNANCE_REPORT — the per-skill governance report artifact (report_md + skill
    # verdicts) persisted by the governance phase for the UI / MR note / standalone
    # file. Free-form per-stage store, keyed off the governance review.
    "GOVERNANCE_REPORT":  ["GOVERNANCE_REVIEW"],
    # VERIFIED_DIFF — the real, compiled, test-green diff the human approves at the
    # relocated design/solution-approval HITL gate ("decide before the gate" shift-left)
    # and that the post-gate APPLYING stage deterministically re-applies. Upstream is
    # REVIEW (end-gate overhaul 2026-07-23): the pre-apply gate NO LONGER depends on
    # governance — governance is now a post-COMMITTING end-gate, so REVIEW → gate is the
    # order. Free-form per-stage store.
    "VERIFIED_DIFF":      ["REVIEW"],
    # COMMITTING upstream is REVIEW (end-gate overhaul 2026-07-23): governance moved to
    # the tail, so COMMITTING no longer depends on GOVERNANCE_REVIEW.
    "COMMITTING":         ["REVIEW"],
    # GOVERNANCE_SCAN — dual role:
    #   (1) standalone governance pipeline (run_type="governance"): the ROOT stage.
    #   (2) inline END-GATE (feature/bug, end-gate overhaul 2026-07-23): sequenced as
    #       the TAIL of SHARED_SM_STAGES, run AFTER COMMITTING + a draft MR_CREATION
    #       (see _run_governance_endgate). It is kept as a DAG root (no upstream) because
    #       the two invocation contexts share this one key; the inline re-scan loop is
    #       driven explicitly by the author/controller, NOT by the artifact cascade, and
    #       each scan writes an immutable sdlc_governance_scan_snapshots row. A missing
    #       upstream contributes "" to the input hash — harmless for this free-form store.
    "GOVERNANCE_SCAN":      [],
    "GOVERNANCE_APPROVAL":  ["GOVERNANCE_SCAN"],
    "GOVERNANCE_FIX":       ["GOVERNANCE_APPROVAL"],
    "GOVERNANCE_REVERIFY":  ["GOVERNANCE_FIX"],
    "MR_CREATION":          ["COMMITTING"],
}

# Stages that MUST produce an artifact before the pipeline can advance.
# GOVERNANCE_REVIEW is deliberately NOT here — it is opt-in/waivable.
MANDATORY_STAGES: set = {"CLASSIFYING", "IMPLEMENT", "COMMITTING"}

# Stages that are legitimately absent on some run configurations and whose
# missing artifact must NOT be treated as "incomplete pipeline".  Specifically,
# GOVERNANCE_SCAN is opt-in/waivable: when governance is disabled the tail stage
# produces no artifact and the pipeline is still considered complete.  Consumers
# (completeness checks, resume guards, UI progress bars) must call
# is_optional_stage() before flagging an absent artifact as a hard gap.
OPTIONAL_STAGES: set = {"GOVERNANCE_SCAN"}


def is_optional_stage(stage: str) -> bool:
    """Return True if *stage* is allowed to produce no artifact on a
    governance-disabled (or otherwise waived) run.  Callers must not treat the
    absence of an optional-stage artifact as an incomplete pipeline."""
    return stage in OPTIONAL_STAGES


# ---------------------------------------------------------------------------
# Run-type-native stage sequences (USER-FACING resume / go-back).
#
# The feature and bug pipelines diverge in their pre-state-machine phases and
# only converge on the shared CodingStateMachine tail. STAGE_DAG above is kept
# feature-shaped because it drives the artifact cascade's upstream hashing,
# which only meaningfully touches the shared SM tail (identical for both types).
# These sequences drive what the resume API and UI expose per run_type.
#
# Notes:
#   - Gate-reorder (2026-07-02): CLASSIFY moved off the CLI-classify-is-mandatory-
#     at-trigger-time footing — it now runs as its own resumable CLI phase
#     (WS-1), so NORMALIZE and CLASSIFYING are both individually go-back-able
#     via resume_from_stage, same as PLAN. Go-back on any of the three routes
#     through _invalidate_from + run_pre_sm_resume_job (agents/sdlc_pipeline.py),
#     NOT the SM-based resume_from_stage_job.
#   - The bug pipeline's "TRIAGING" transition is a non-LLM inbox-notification
#     step with no re-runnable output, so it is deliberately NOT a resume target.
#   - Three-phase cutover: BOTH run types share the same pre-SM head now —
#     NORMALIZE → CLASSIFYING → PLAN (bugs unified into PLAN; the legacy feature
#     ANALYZING/DESIGNING and bug TROUBLESHOOTING/SOLUTIONING phases are gone).
#     The shared SM tail is the collapsed IMPLEMENT (codegen+compile+test) →
#     REVIEW (Opus diff-only gate) → COMMITTING.
# ---------------------------------------------------------------------------
PRE_SM_STAGES_BY_TYPE: dict = {
    "feature":    ["NORMALIZE", "CLASSIFYING", "PLAN"],
    "bug":        ["NORMALIZE", "CLASSIFYING", "PLAN"],
    "governance": ["GOVERNANCE_SCAN"],   # standalone governance pipeline entry point
}
# End-gate overhaul (2026-07-23): GOVERNANCE_REVIEW removed from the mid-tail;
# GOVERNANCE_SCAN is now the TAIL stage — governance fires AFTER COMMITTING (and a
# draft MR) as the end-gate before merge, not before APPLYING. AWAITING_GOVERNANCE_
# APPROVAL remains a gate-only state (not in STAGE_DAG/MANDATORY_STAGES).
SHARED_SM_STAGES: list = ["IMPLEMENT", "REVIEW", "TEST_VERIFY", "COMMITTING", "GOVERNANCE_SCAN"]


def stage_sequence_for(run_type: str) -> list:
    """Ordered resumable stage list for a run_type: pre-SM head + shared SM tail.
    Both run types now share the same head (CLASSIFYING → PLAN) after the
    three-phase cutover.  The governance run type has its own dedicated sequence
    (GOVERNANCE_SCAN → GOVERNANCE_APPROVAL → GOVERNANCE_FIX → GOVERNANCE_REVERIFY
    → COMMITTING → MR_CREATION) that does not pass through the shared SM tail.

    Note: AWAITING_GOVERNANCE_APPROVAL is a recognised HITL gate state for the
    governance pipeline; it is not listed in STAGE_DAG or MANDATORY_STAGES because
    it is a UI-gate-only state (not a resumable artifact stage).
    """
    rt = (run_type or "feature").lower()
    if rt == "governance":
        return [
            "GOVERNANCE_SCAN",
            "GOVERNANCE_APPROVAL",
            "GOVERNANCE_FIX",
            "GOVERNANCE_REVERIFY",
            "COMMITTING",
            "MR_CREATION",
        ]
    head = PRE_SM_STAGES_BY_TYPE.get(rt, PRE_SM_STAGES_BY_TYPE["feature"])
    return head + SHARED_SM_STAGES


def pre_sm_revision_stages(run_type: str) -> set:
    """Pre-state-machine stages that resume via the pre-SM revision path (go-back
    invalidates the stage + everything downstream via _invalidate_from, then
    re-enters _drive_pre_sm at that stage — see resume_from_stage). Gate-reorder
    (2026-07-02): CLASSIFYING is no longer the mandatory un-resumable root — it
    is its own CLI phase (WS-1) — so the FULL pre-SM head is revisable."""
    head = PRE_SM_STAGES_BY_TYPE.get(
        (run_type or "feature").lower(), PRE_SM_STAGES_BY_TYPE["feature"]
    )
    return set(head)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_session():
    try:
        from db.database import SessionLocal
        return SessionLocal()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _store_artifact(
    run_id:     str,
    stage:      str,
    payload:    dict,
    producer:   str,
    input_hash: str,
    created_by: str,
    reason:     str  = None,
    score:      float = None,
) -> None:
    """
    Insert a new versioned artifact row for (run_id, stage).

    The version is computed as MAX(version)+1 for that run+stage pair so that
    re-runs create a new row rather than overwriting the previous output.
    On any exception: logs a warning and returns without re-raising so that
    a persistence failure never aborts the pipeline.
    """
    session = _get_session()
    if session is None:
        logger.warning("sdlc_artifacts: no DB session — artifact not stored")
        return
    try:
        row = session.execute(
            text(
                "SELECT COALESCE(MAX(version), 0) AS max_ver "
                "FROM sdlc_stage_artifacts "
                "WHERE run_id = :r AND stage = :s"
            ),
            {"r": run_id, "s": stage},
        ).fetchone()
        next_version = (row.max_ver if row else 0) + 1

        session.execute(
            text(
                "INSERT INTO sdlc_stage_artifacts "
                "(run_id, stage, version, status, payload, input_hash, "
                " producer, score, created_by, reason) "
                "VALUES (:run_id, :stage, :version, 'PRODUCED', CAST(:payload AS jsonb), "
                "        :input_hash, :producer, :score, :created_by, :reason)"
            ),
            {
                "run_id":     run_id,
                "stage":      stage,
                "version":    next_version,
                "payload":    json.dumps(payload),
                "input_hash": input_hash,
                "producer":   producer,
                "score":      score,
                "created_by": created_by,
                "reason":     reason,
            },
        )
        session.commit()
        logger.info(
            f"sdlc_artifacts: stored artifact run={run_id} stage={type(stage).__name__} "
            f"version={next_version} (payload_bytes={len(json.dumps(payload))})"
        )
    except Exception:
        logger.warning(f"sdlc_artifacts: _store_artifact failed for {run_id}/{stage} → {type(exc).__name__}")
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()


def _load_latest_artifact(run_id: str, stage: str):
    """
    Return the highest-version artifact for (run_id, stage) as a dict, or
    None if no row exists or on any error.  The payload column is returned
    as a parsed Python dict (JSONB auto-deserializes; guard against string).
    """
    session = _get_session()
    if session is None:
        return None
    try:
        row = session.execute(
            text(
                "SELECT id, run_id, stage, version, status, payload, "
                "       input_hash, producer, score, skills_used, "
                "       created_by, reason, created_at "
                "FROM sdlc_stage_artifacts "
                "WHERE run_id = :r AND stage = :s "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"r": run_id, "s": stage},
        ).fetchone()
        if row is None:
            return None

        # JSONB columns auto-deserialize in psycopg2; guard against str.
        payload = row.payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        skills_used = row.skills_used
        if isinstance(skills_used, str):
            try:
                skills_used = json.loads(skills_used)
            except Exception:
                skills_used = None

        return {
            "id":          str(row.id),
            "run_id":      str(row.run_id),
            "stage":       row.stage,
            "version":     row.version,
            "status":      row.status,
            "payload":     payload,
            "input_hash":  row.input_hash,
            "producer":    row.producer,
            "score":       row.score,
            "skills_used": skills_used,
            "created_by":  row.created_by,
            "reason":      row.reason,
            "created_at":  row.created_at.isoformat() if row.created_at else None,
        }
    except Exception:
        logger.warning(f"sdlc_artifacts: _load_latest_artifact failed for {run_id}/{stage} → {type(exc).__name__}")
        return None
    finally:
        session.close()


def _mark_stale(run_id: str, stage: str) -> None:
    """
    Mark all PRODUCED artifacts for (run_id, stage) as STALE.
    Used when a stage is re-run to signal that prior output is superseded.
    On any exception: logs a warning and returns without re-raising.
    """
    session = _get_session()
    if session is None:
        logger.warning("sdlc_artifacts: no DB session — _mark_stale skipped")
        return
    try:
        session.execute(
            text(
                "UPDATE sdlc_stage_artifacts "
                "SET status = 'STALE' "
                "WHERE run_id = :r AND stage = :s AND status = 'PRODUCED'"
            ),
            {"r": run_id, "s": stage},
        )
        session.commit()
    except Exception:
        logger.warning(f"sdlc_artifacts: _mark_stale failed for {run_id}/{stage} → {type(exc).__name__}")
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()


def compute_input_hash(run_id: str, stage: str) -> str:
    """
    PURE READ-ONLY.  Derive a deterministic hash that captures the inputs to a
    stage so that unchanged inputs can be detected and the cached artifact
    reused.

    Algorithm:
      1. For each upstream stage in STAGE_DAG[stage], load the latest artifact
         and collect its id (or empty string if absent).
      2. Fetch the run's language, repo, and jira_key from sdlc_store.get_run().
      3. SHA256 over the concatenation of upstream_ids + run config fields.

    Returns the full 64-character hex digest.  On any exception, falls back to
    SHA256 of "{run_id}:{type(stage).__name__}" truncated to 32 characters so the pipeline
    never blocks on a hash failure.
    """
    try:
        upstream_stages = STAGE_DAG.get(stage, [])

        upstream_ids = []
        for up_stage in upstream_stages:
            art = _load_latest_artifact(run_id, up_stage)
            upstream_ids.append(art["id"] if art else "")

        # Fetch run config for language, repo, jira_key
        from store.sdlc_store import get_run
        run = get_run(run_id) or {}
        repo_ctx = (run.get("context") or {}).get("repo_ctx") or {}
        language  = repo_ctx.get("language", "")
        repo      = run.get("repo", "")
        jira_key  = run.get("jira_key", "")

        raw = "|".join(upstream_ids) + f"|{language}|{repo}|{type(jira_key).__name__}"
        return hashlib.sha256(raw.encode()).hexdigest()
    except Exception:
        logger.warning(f"sdlc_artifacts: compute_input_hash fallback for {run_id}/{stage} → {type(exc).__name__}")
        return hashlib.sha256(f"{run_id}:{type(stage).__name__}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Risk scoring
# ---------------------------------------------------------------------------

_PATH_SENSITIVITY_RE = re.compile(
    r"payment|auth|compliance|security|pci|aadhaar|upi|pin|cvv|pan",
    re.IGNORECASE,
)


def compute_risk_score(
    complexity:      str,
    files_to_change: int,
    new_files:       int  = None,
    file_paths:      list = None,
) -> float:
    """
    Compute a 0.0–1.0 risk score for an SDLC run.

    Three additive factors:
    - Complexity base:   simple=0.1, medium=0.4, complex=0.7
    - Blast radius:      min(0.3, files_to_change × 0.03)
    - Path sensitivity:  +0.3 if any path matches payment|auth|compliance|
                         security|pci|aadhaar|upi|pin|cvv|pan

    Returns min(1.0, sum_of_factors).
    """
    _BASE = {"simple": 0.1, "medium": 0.4, "complex": 0.7}
    base = _BASE.get((complexity or "medium").lower().strip(), 0.4)

    total = max(0, int(files_to_change or 0))
    blast = min(0.3, total * 0.03)

    sensitive = 0.0
    if file_paths:
        for p in file_paths:
            if _PATH_SENSITIVITY_RE.search(str(p)):
                sensitive = 0.3
                break

    return min(1.0, base + blast + sensitive)
