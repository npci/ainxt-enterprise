# SPDX-License-Identifier: Apache-2.0
"""
workflows/planner.py — P8: Planning engine.

Provides DAG analysis, critical path computation, risk estimation,
plan checkpointing, and rollback-aware execution.

DESIGN
------
- Critical path: standard CPM (topological sort → forward pass → backward pass)
  Nodes where slack == 0 are on the critical path.
- Risk estimation: heuristic (no LLM) based on step_type + critical path + rollback_fn
- Checkpointing: Redis key per workflow_id, TTL=PLAN_CHECKPOINT_TTL_SEC (default 86400)
- Rollback: execute rollback_fn() in reverse order for completed steps on failure

WHAT IS NOT BUILT
-----------------
- Distributed locking for plan checkpoints
- Plan versioning
- LLM-based risk estimation
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.logger import logger


_PLAN_CHECKPOINT_TTL = int(os.getenv("PLAN_CHECKPOINT_TTL_SEC", "86400"))

# Risk heuristics by step_type (base scores)
_STEP_TYPE_RISK = {
    "code":     0.7,
    "shell":    0.7,
    "tool":     0.5,
    "llm":      0.3,
    "approval": 0.1,
}


@dataclass
class PlanAnalysis:
    """Result of PlanningEngine.analyze_plan()."""
    critical_path:          List[str]   # step IDs on the critical path
    total_risk:             float       # aggregate risk score 0.0–1.0
    estimated_duration_sec: float       # sum of timeout_sec for critical path steps
    rollback_order:         List[str]   # step IDs in reverse execution order (for rollback)
    step_risks:             Dict[str, float] = field(default_factory=dict)
    step_slack:             Dict[str, float] = field(default_factory=dict)


class PlanningEngine:
    """
    Analyzes workflow DAGs, computes critical paths, estimates risk,
    and provides checkpoint/resume + rollback-aware execution.
    """

    # ── Critical path (CPM) ──────────────────────────────────────────────────

    def analyze_plan(self, steps: list) -> PlanAnalysis:
        """
        Analyze a list of WorkflowStep objects.

        Returns PlanAnalysis with:
          - critical_path: step IDs where slack == 0
          - total_risk: weighted average of step risks
          - estimated_duration_sec: sum of timeout_sec on critical path
          - rollback_order: reverse topological order of all steps
        """
        if not steps:
            return PlanAnalysis(
                critical_path=[], total_risk=0.0,
                estimated_duration_sec=0.0, rollback_order=[],
            )

        # Build adjacency: step_id → list of successor step_ids
        step_map = {s.id: s for s in steps}
        successors: Dict[str, List[str]] = {s.id: [] for s in steps}
        predecessors: Dict[str, List[str]] = {s.id: list(s.depends_on) for s in steps}
        for s in steps:
            for dep in s.depends_on:
                if dep in successors:
                    successors[dep].append(s.id)

        # Topological sort (Kahn's algorithm)
        in_degree = {s.id: len(s.depends_on) for s in steps}
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        topo_order: List[str] = []
        while queue:
            node = queue.pop(0)
            topo_order.append(node)
            for succ in successors[node]:
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    queue.append(succ)

        if len(topo_order) != len(steps):
            # Cycle detected — return safe defaults
            logger.warning("PlanningEngine: cycle detected in workflow DAG, skipping CPM")
            return PlanAnalysis(
                critical_path=[s.id for s in steps],
                total_risk=0.5,
                estimated_duration_sec=sum(s.timeout_sec for s in steps),
                rollback_order=[s.id for s in reversed(steps)],
            )

        # Forward pass: earliest start time (EST) and earliest finish time (EFT)
        est: Dict[str, float] = {}
        eft: Dict[str, float] = {}
        for sid in topo_order:
            step = step_map[sid]
            duration = float(step.timeout_sec)
            if not predecessors[sid]:
                est[sid] = 0.0
            else:
                est[sid] = max(eft.get(p, 0.0) for p in predecessors[sid])
            eft[sid] = est[sid] + duration

        project_duration = max(eft.values()) if eft else 0.0

        # Backward pass: latest start time (LST) and latest finish time (LFT)
        lft: Dict[str, float] = {}
        lst: Dict[str, float] = {}
        for sid in reversed(topo_order):
            step = step_map[sid]
            duration = float(step.timeout_sec)
            if not successors[sid]:
                lft[sid] = project_duration
            else:
                lft[sid] = min(lst.get(s, project_duration) for s in successors[sid])
            lst[sid] = lft[sid] - duration

        # Slack = LST - EST; critical path = slack == 0
        slack: Dict[str, float] = {sid: lst[sid] - est[sid] for sid in topo_order}
        critical_path = [sid for sid in topo_order if abs(slack[sid]) < 0.001]

        # Mark critical path on steps
        for s in steps:
            s.is_critical_path = s.id in critical_path

        # Risk estimation
        step_risks: Dict[str, float] = {}
        for s in steps:
            risk = self.estimate_step_risk(s)
            step_risks[s.id] = risk
            s.risk_score = risk

        total_risk = (
            sum(step_risks[sid] for sid in critical_path) / len(critical_path)
            if critical_path else 0.0
        )
        total_risk = max(0.0, min(1.0, total_risk))

        critical_duration = sum(
            step_map[sid].timeout_sec for sid in critical_path
        )

        rollback_order = list(reversed(topo_order))

        return PlanAnalysis(
            critical_path=critical_path,
            total_risk=total_risk,
            estimated_duration_sec=critical_duration,
            rollback_order=rollback_order,
            step_risks=step_risks,
            step_slack=slack,
        )

    def estimate_step_risk(self, step) -> float:
        """
        Heuristic risk score for a single step (no LLM call).

        Base score by step_type:
          code/shell → 0.7, tool → 0.5, llm → 0.3, approval → 0.1

        Multipliers:
          on critical path → ×1.5
          no rollback_fn   → ×1.3

        Clamped to [0.0, 1.0].
        """
        base = _STEP_TYPE_RISK.get(step.step_type, 0.5)
        if step.is_critical_path:
            base *= 1.5
        if step.rollback_fn is None:
            base *= 1.3
        return max(0.0, min(1.0, base))

    # ── Checkpointing ────────────────────────────────────────────────────────

    def checkpoint_plan(self, workflow_id: str, plan_state: dict) -> None:
        """
        Save plan state to Redis.
        Key: plan_ckpt:{workflow_id}
        TTL: PLAN_CHECKPOINT_TTL_SEC (default 86400s = 24h)
        """
        try:
            from core.kv import get_kv
            from core.config import RDB_CACHE
            redis = get_kv(RDB_CACHE, decode_responses=True)
            key = f"plan_ckpt:{workflow_id}"
            redis.setex(key, _PLAN_CHECKPOINT_TTL, json.dumps({
                **plan_state,
                "_checkpoint_ts": time.time(),
            }))
            logger.debug(f"PlanningEngine: checkpointed plan {workflow_id}")
        except Exception as e:
            logger.warning(f"PlanningEngine.checkpoint_plan failed (non-fatal): {e}")

    def resume_plan(self, workflow_id: str) -> Optional[dict]:
        """
        Load plan checkpoint from Redis.
        Returns None if no checkpoint exists or it has expired.
        """
        try:
            from core.kv import get_kv
            from core.config import RDB_CACHE
            redis = get_kv(RDB_CACHE, decode_responses=True)
            key = f"plan_ckpt:{workflow_id}"
            raw = redis.get(key)
            if raw:
                state = json.loads(raw)
                logger.info(f"PlanningEngine: resumed plan {workflow_id} from checkpoint")
                return state
        except Exception as e:
            logger.warning(f"PlanningEngine.resume_plan failed (non-fatal): {e}")
        return None

    # ── Rollback-aware execution ─────────────────────────────────────────────

    def execute_with_rollback(
        self,
        workflow_id: str,
        steps: list,
        executor_fn: Callable[[Any], Any],
        stop_on_failure: bool = True,
    ) -> dict:
        """
        Execute steps with checkpoint-after-each-step and rollback on failure.

        Algorithm:
          1. analyze_plan() → compute critical path + risk
          2. checkpoint_plan() with initial state
          3. For each step (topological order):
             a. Skip if already completed (resume path)
             b. Execute via executor_fn(step)
             c. checkpoint_plan() after success
             d. On failure: execute rollback_fn() in reverse order for completed steps
          4. Return execution summary

        executor_fn: callable(step) → result (str or dict)
        Returns: {"completed": [...], "failed": [...], "rolled_back": [...], "plan": PlanAnalysis}
        """
        analysis = self.analyze_plan(steps)
        logger.info(
            f"PlanningEngine: executing workflow={workflow_id} "
            f"steps={len(steps)} critical_path={analysis.critical_path} "
            f"total_risk={analysis.total_risk:.2f}"
        )

        # Check for existing checkpoint (resume path)
        checkpoint = self.resume_plan(workflow_id)
        completed_ids = set(checkpoint.get("completed_steps", [])) if checkpoint else set()
        if completed_ids:
            logger.info(
                f"PlanningEngine: resuming from checkpoint — "
                f"already completed: {sorted(completed_ids)}"
            )

        completed: List[str] = list(completed_ids)
        failed: List[str] = []
        results: Dict[str, Any] = checkpoint.get("results", {}) if checkpoint else {}

        # Topological execution order
        step_map = {s.id: s for s in steps}
        topo_order = analysis.rollback_order[::-1]  # rollback_order is reversed topo

        for sid in topo_order:
            if sid in completed_ids:
                continue  # already done (resume path)

            step = step_map.get(sid)
            if step is None:
                continue

            # Check all dependencies are satisfied
            if not all(dep in completed_ids for dep in step.depends_on):
                if stop_on_failure:
                    logger.warning(
                        f"PlanningEngine: step {sid} dependencies not met "
                        f"(missing: {[d for d in step.depends_on if d not in completed_ids]})"
                    )
                    failed.append(sid)
                    break
                continue

            try:
                logger.info(f"PlanningEngine: executing step {sid} ({step.name})")
                result = executor_fn(step)
                results[sid] = str(result)[:500]
                completed.append(sid)
                completed_ids.add(sid)

                # Checkpoint after each successful step
                self.checkpoint_plan(workflow_id, {
                    "completed_steps": completed,
                    "results": results,
                    "workflow_id": workflow_id,
                })

            except Exception as e:
                logger.error(f"PlanningEngine: step {sid} failed: {e}")
                failed.append(sid)

                # Execute rollback in reverse order for completed steps
                rolled_back: List[str] = []
                for rollback_sid in analysis.rollback_order:
                    if rollback_sid not in completed_ids:
                        continue
                    rb_step = step_map.get(rollback_sid)
                    if rb_step and rb_step.rollback_fn:
                        try:
                            rb_step.rollback_fn()
                            rolled_back.append(rollback_sid)
                            logger.info(f"PlanningEngine: rolled back step {rollback_sid}")
                        except Exception as rb_e:
                            logger.error(
                                f"PlanningEngine: rollback failed for step {rollback_sid}: {rb_e}"
                            )

                return {
                    "completed":    completed,
                    "failed":       failed,
                    "rolled_back":  rolled_back,
                    "plan":         analysis,
                    "error":        str(e),
                }
                # SEC-16: removed unreachable `if stop_on_failure: break` (dead code after return)

        return {
            "completed":   completed,
            "failed":      failed,
            "rolled_back": [],
            "plan":        analysis,
            "error":       None,
        }
