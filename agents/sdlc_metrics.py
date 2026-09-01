# SPDX-License-Identifier: Apache-2.0
"""
sdlc_metrics.py — Read-side analytics for the three-phase SDLC CLI engine.

This module is READ-ONLY. It does NOT alter pipeline control flow and does NOT
import or call anything that mutates state. It derives per-run metrics from the
existing artifacts (PLAN, VERIFIED_DIFF) and the run-event audit trail already
written by `store.sdlc_store.add_run_event`.

Three-phase engine (2026-07-01 hard cutover)
--------------------------------------------
The old ANALYZING/DESIGNING/DIAGNOSING/CODING stages, the `agent-loop` navigator
and the completeness verifier were all deleted. The pipeline is now:

    PLAN (read-only planner) → IMPLEMENT (one session: code + tests + green)
    → REVIEW (platform Opus over the diff only; one bounded fix round)

Every metric below maps to data that phase set actually produces:

  files_predicted        PLAN artifact: files_to_change + new_files_needed
  files_modified         VERIFIED_DIFF: number of edited files (n_edits)
  plan_hits              predicted files that actually appear in the diff
  off_plan_files         diff files that were NOT predicted (scope-creep signal)
  open_questions         PLAN artifact: len(open_questions)
  rounds_to_converge     number of REVIEW passes (1 = clean approve, 2 = one fix round)
  review_approved        REVIEW event verdict
  review_blocking_issues REVIEW event blocking count
  compile_passed         VERIFIED_DIFF event: compile gate result
  tests_passed           VERIFIED_DIFF event: test gate result
  tests_deferred         tests authored pre-gate, executed post-gate (Option 2)
  event_count_by_stage   {stage_name: count} of scanned events

Fields that cannot be populated honestly under the CLI engine (the CLI does not
report which files it read, and there is no navigator/verifier loop) are NOT
emitted — they were the source of the "always null" metrics and have been removed.

Public API
----------
compute_run_metrics(run_id) -> dict
    Load artifacts + events for run_id and return the metrics dict. Primary entry.

compute_exploration_metrics_for_run(run_id, stage=None) -> dict
    Backwards-compatible alias for compute_run_metrics (the `stage` filter is no
    longer meaningful under the three-phase engine and is accepted-but-ignored).

compute_exploration_metrics(events, stage=None) -> dict
    Event-only subset (review rounds/verdict, verified-diff gate status, event
    counts). Kept for callers that only have a pre-fetched event list.

log_exploration_metrics(run_id, stage=None) -> dict
    Compute then emit a single structured INFO log tagged "[SDLC-METRICS]".
"""

from __future__ import annotations

from typing import Any, Optional

from core.logger import logger


# ── Helpers ─────────────────────────────────────────────────────────────────

def _safe_list(val: Any) -> list:
    """Coerce a value to a list; returns [] on None / non-list."""
    return val if isinstance(val, list) else []


def _safe_int(val: Any, default: Optional[int] = 0) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _norm_path(p: Any) -> Optional[str]:
    """Normalize a path-ish value to a comparable string, or None."""
    if isinstance(p, str) and p.strip():
        s = p.strip().replace("\\", "/")
        while s.startswith("./"):
            s = s[2:]
        return s.lstrip("/")
    if isinstance(p, dict):
        return _norm_path(p.get("path") or p.get("file") or p.get("name"))
    return None


def _path_set(items: Any) -> set:
    """Extract a normalized set of path strings from a list of str/dict entries."""
    out: set = set()
    for item in _safe_list(items):
        np = _norm_path(item)
        if np:
            out.add(np)
    return out


# ── Event-derived metrics (no artifact access) ────────────────────────────────

def compute_exploration_metrics(
    events: list[dict],
    stage: Optional[str] = None,   # accepted for API compat; not used
) -> dict:
    """Derive the event-only subset of run metrics from a pre-fetched event list.

    Covers what the run-event trail alone can tell us: REVIEW rounds + verdict,
    the VERIFIED_DIFF gate status (compile/tests + edited-file count), and a
    per-stage event breakdown. Artifact-derived fields (files_predicted,
    plan adherence, open_questions) are None here — use ``compute_run_metrics``
    when a run_id is available.
    """
    files_modified: Optional[int] = None
    compile_passed: Optional[bool] = None
    tests_passed: Optional[bool] = None
    review_rounds = 0
    review_approved: Optional[bool] = None
    review_blocking_issues: Optional[int] = None
    event_count_by_stage: dict = {}

    for ev in _safe_list(events):
        ev_stage = (ev.get("stage") or "").upper()
        ev_data: dict = ev.get("data") or {}

        if ev_stage:
            event_count_by_stage[ev_stage] = event_count_by_stage.get(ev_stage, 0) + 1

        # VERIFIED_DIFF: edited-file count + compile/test gate status.
        if ev_stage == "VERIFIED_DIFF":
            _n = _safe_int(ev_data.get("n_edits"), None)
            if _n is not None:
                files_modified = _n
            if isinstance(ev_data.get("compile_passed"), bool):
                compile_passed = ev_data["compile_passed"]
            if isinstance(ev_data.get("tests_passed"), bool):
                tests_passed = ev_data["tests_passed"]

        # REVIEW: each pass emits one event. The LAST verdict is authoritative.
        if ev_stage == "REVIEW":
            review_rounds += 1
            if isinstance(ev_data.get("approved"), bool):
                review_approved = ev_data["approved"]
            _b = _safe_int(ev_data.get("blocking"), None)
            if _b is not None:
                review_blocking_issues = _b

    return {
        "files_modified":         files_modified,
        "compile_passed":         compile_passed,
        "tests_passed":           tests_passed,
        "rounds_to_converge":     review_rounds or None,
        "review_approved":        review_approved,
        "review_blocking_issues": review_blocking_issues,
        "event_count_by_stage":   event_count_by_stage,
    }


# ── Full run metrics (artifacts + events) ──────────────────────────────────────

def compute_run_metrics(run_id: str) -> dict:
    """Load PLAN + VERIFIED_DIFF artifacts and the event trail for ``run_id`` and
    return the full three-phase metrics dict.

    Returns a dict with an ``error`` key (and no metric keys) on any fetch failure
    so the caller can log-and-degrade rather than raise.
    """
    try:
        from store.sdlc_store import get_run_events
        events = get_run_events(run_id)
    except Exception as exc:
        logger.warning(
            "[SDLC-METRICS] get_run_events failed — returning empty metrics",
            run_id=run_id, error=str(exc),
        )
        return {"error": str(exc), "run_id": run_id}

    metrics = compute_exploration_metrics(events)

    # ── Artifact-derived fields ───────────────────────────────────────────────
    files_predicted: Optional[int] = None
    open_questions: Optional[int] = None
    tests_deferred: Optional[bool] = None
    plan_hits: Optional[int] = None
    off_plan_files: Optional[int] = None
    _plan_art = None
    _vd_art = None
    _predicted_paths: set = set()
    _vd_edits = 0

    try:
        from store.sdlc_artifacts import _load_latest_artifact

        _plan_art = _load_latest_artifact(run_id, "PLAN")
        plan = (_plan_art or {}).get("payload") or {}
        _predicted_paths = _path_set(plan.get("files_to_change")) | _path_set(plan.get("new_files_needed"))
        if _predicted_paths:
            files_predicted = len(_predicted_paths)
        oq = plan.get("open_questions")
        if isinstance(oq, list):
            open_questions = len(oq)

        _vd_art = _load_latest_artifact(run_id, "VERIFIED_DIFF")
        vd = (_vd_art or {}).get("payload") or {}
        # files_modified (headline): total edited files. Prefer the rich `edits`
        # list, fall back to code + slt file lists.
        _vd_edits = len(_safe_list(vd.get("edits")))
        all_modified = _path_set([e.get("path") for e in _safe_list(vd.get("edits"))])
        if not all_modified:
            all_modified = _path_set(vd.get("files")) | _path_set(vd.get("slt_files"))
        if all_modified and metrics.get("files_modified") is None:
            metrics["files_modified"] = len(all_modified)
        # Plan adherence: compare the plan's predicted files against the CODE files in
        # the diff only. Test/SLT files authored by IMPLEMENT are expected and rarely
        # pre-listed in the plan, so counting them would make off-plan noisy.
        code_modified = _path_set(vd.get("files")) or all_modified
        if _predicted_paths and code_modified:
            plan_hits = len(_predicted_paths & code_modified)
            off_plan_files = len(code_modified - _predicted_paths)
        _tests = vd.get("tests") or {}
        if isinstance(_tests, dict) and isinstance(_tests.get("deferred"), bool):
            tests_deferred = _tests["deferred"]
    except Exception as exc:
        logger.warning(f"[SDLC-METRICS] artifact-derived metrics unavailable for {run_id}: {exc}")

    # Pinpoint diagnostic: with two independent read paths (events + artifacts),
    # an all-null result means BOTH came back empty. This one line says which —
    # whether events were read at all and which stages, and whether the PLAN /
    # VERIFIED_DIFF artifacts were found for THIS run_id.
    logger.info(
        "[SDLC-METRICS] compute diagnostics",
        run_id=run_id,
        n_events=len(events or []),
        event_stages=sorted((metrics.get("event_count_by_stage") or {}).keys()),
        plan_art_found=bool(_plan_art),
        plan_predicted_files=len(_predicted_paths),
        verified_diff_art_found=bool(_vd_art),
        verified_diff_edits=_vd_edits,
    )

    metrics.update({
        "files_predicted": files_predicted,
        "open_questions":  open_questions,
        "plan_hits":       plan_hits,
        "off_plan_files":  off_plan_files,
        "tests_deferred":  tests_deferred,
    })
    return metrics


def compute_exploration_metrics_for_run(
    run_id: str,
    stage: Optional[str] = None,   # accepted for API compat; not used
) -> dict:
    """Backwards-compatible alias for :func:`compute_run_metrics`.

    The historical ``stage`` filter is no longer meaningful under the three-phase
    engine (metrics are whole-run) and is accepted-but-ignored.
    """
    return compute_run_metrics(run_id)


# ── Convenience logger ───────────────────────────────────────────────────────

def log_exploration_metrics(
    run_id: str,
    stage: Optional[str] = None,
) -> dict:
    """Compute run metrics and emit a single structured INFO log tagged
    "[SDLC-METRICS]". Returns the metrics dict."""
    metrics = compute_run_metrics(run_id)
    logger.info(
        "[SDLC-METRICS] run metrics",
        run_id=run_id,
        **metrics,
    )
    return metrics
