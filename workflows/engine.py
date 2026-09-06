# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt WORKFLOW ENGINE  (Phase 8 + SDLC upgrade)
# Multi-step agent workflows with DAG execution,
# task chaining, PCI compliance, and memory persistence.
# ============================================================
#
# Step types:
#   llm      — call model_router.generate(); input templated with {outputs}
#   code     — execute Python in Docker via SelfHealingEngine
#   shell    — execute bash in Docker via DockerExecutor
#   tool     — call a registered Python callable
#   approval — pause workflow; publish inbox item; resume via resume()
#
# Chaining:
#   Each step can declare depends_on: [step_id, ...].
#   Steps with no dependencies run first (in definition order).
#   Outputs of prior steps are injected into later step inputs
#   via {step_id} placeholders.
#   Independent steps at the same DAG level run IN PARALLEL via
#   ThreadPoolExecutor (up to PARALLEL_WORKERS workers).
#
# Failure modes:
#   stop_on_failure=True  → halt workflow on first failed step
#   stop_on_failure=False → continue remaining steps, mark failed
#
# HITL / Approval:
#   step_type="approval" pauses execution, persists state, and returns
#   WorkflowResult with paused=True. Call engine.resume(workflow_id,
#   approved=True/False, feedback="...") to continue.
#
# ============================================================

import contextvars
import json
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

PARALLEL_WORKERS   = 4    # max threads for parallel step execution
STEP_TIMEOUT_SEC   = 300  # default per-step timeout (5 minutes)

from core.logger import logger
from core.config import REDIS_HOST as _REDIS_HOST, REDIS_PORT as _REDIS_PORT
from agents.compliance_engine import compliance_engine


# ============================================================
# STEP DEFINITION
# ============================================================

@dataclass
class WorkflowStep:
    """
    A single unit of work inside a workflow.

    Fields:
        id          Unique identifier within the workflow.
        name        Human-readable label.
        step_type   "llm" | "code" | "shell" | "tool"
        input       Prompt/code/command to execute.
                    May contain {step_id} placeholders that are
                    replaced with the output of the named step.
        depends_on  List of step IDs that must complete first.
        tool_fn     For step_type="tool", the Python callable.
        heal        Whether to use SelfHealingEngine for code steps.
                    Default True.
        metadata    Arbitrary user data stored in execution record.
    """

    id:         str
    name:       str
    step_type:  str              # "llm" | "code" | "shell" | "tool" | "approval"
    input:      str = ""
    depends_on: List[str] = field(default_factory=list)
    tool_fn:    Optional[Callable] = None
    heal:       bool = True
    metadata:   Dict[str, Any] = field(default_factory=dict)
    timeout_sec: int = STEP_TIMEOUT_SEC  # per-step wall-clock timeout
    # approval-step extras
    approval_message: str = ""   # message shown in inbox notification
    notify_user:      str = "team"  # inbox user_id to notify
    # conditional branching
    branch_on: Optional[Dict[str, str]] = None
    # Maps keyword (case-insensitive) → next step_id to jump to.
    # Example: {"PASS": "deploy_step", "FAIL": "fix_step"}
    # When the step output contains a matching keyword, execution continues
    # from the matched step; all other steps that would have run next are
    # skipped.  If no keyword matches, normal DAG order continues.
    # P8: Planning engine fields
    risk_score:       float = 0.5                # 0.0–1.0 (computed by PlanningEngine)
    rollback_fn:      Optional[Callable] = None  # callable() → None; invoked on failure
    checkpoint_key:   Optional[str] = None       # Redis key for step checkpoint
    is_critical_path: bool = False               # set by PlanningEngine.analyze_plan()


# ============================================================
# WORKFLOW DEFINITION
# ============================================================

@dataclass
class Workflow:
    """
    A named collection of WorkflowSteps forming a DAG.

    Fields:
        name             Workflow name (used in memory keys).
        steps            Ordered list of WorkflowStep objects.
        stop_on_failure  Halt execution on first step failure.
        description      Optional free-text description.
    """

    name:             str
    steps:            List[WorkflowStep] = field(default_factory=list)
    stop_on_failure:  bool = True
    description:      str = ""

    def add_step(self, step: WorkflowStep) -> "Workflow":
        self.steps.append(step)
        return self


# ============================================================
# STEP RESULT
# ============================================================

@dataclass
class StepResult:
    step_id:    str
    step_name:  str
    success:    bool
    output:     str
    error:      Optional[str]
    started_at: str
    ended_at:   str
    attempts:   int = 1


# ============================================================
# WORKFLOW RESULT
# ============================================================

@dataclass
class WorkflowResult:
    workflow_id:       str
    workflow_name:     str
    success:           bool
    steps:             List[StepResult]
    started_at:        str
    ended_at:          str
    metadata:          Dict[str, Any] = field(default_factory=dict)
    # HITL pause fields
    paused:            bool = False
    approval_step_id:  Optional[str] = None
    approval_message:  Optional[str] = None

    def step_output(self, step_id: str) -> Optional[str]:
        for s in self.steps:
            if s.step_id == step_id:
                return s.output
        return None


# ============================================================
# WORKFLOW ENGINE
# ============================================================

class WorkflowEngine:
    """
    Executes Workflow objects.

    Responsibilities:
    - Topological sort of steps (DAG)
    - Input templating (inject prior step outputs)
    - PCI compliance check on each step input and output
    - Step dispatch to LLM, Docker, or tool
    - Memory persistence (Redis if available, silent skip if not)
    - Structured result return
    """

    def __init__(self):
        self._memory = self._init_memory()
        logger.info("WorkflowEngine initialised")

    # --------------------------------------------------------
    # OPTIONAL MEMORY INIT
    # --------------------------------------------------------

    def _init_memory(self):
        try:
            from memory.redis_memory import RedisMemory
            mem = RedisMemory()
            if mem.ping():
                logger.info("WorkflowEngine: Redis memory connected")
                return mem
        except Exception as e:
            logger.warning(f"WorkflowEngine: memory unavailable → {e}")
        return None

    # ========================================================
    # PUBLIC RUN
    # ========================================================

    def run(self, workflow: Workflow, workflow_id: Optional[str] = None) -> WorkflowResult:
        """
        Execute a workflow and return a WorkflowResult.

        Independent steps at the same DAG level run in parallel via
        ThreadPoolExecutor. When a step_type="approval" step is reached
        the workflow pauses: state is persisted, an inbox notification is
        published, and a WorkflowResult with paused=True is returned.
        Call engine.resume(workflow_id, approved, feedback) to continue.
        """
        workflow_id  = workflow_id or str(uuid.uuid4())
        started_at   = datetime.utcnow().isoformat()

        logger.info(f"WorkflowEngine → START wf={workflow.name} id={workflow_id}")
        # Record run start in Postgres for crash/restart recovery
        self._upsert_run_record(workflow_id, workflow.name, "running", None)

        # Validate step graph + group into parallel levels
        try:
            levels = self._group_by_level(workflow)
        except ValueError as e:
            ended_at = datetime.utcnow().isoformat()
            logger.error(f"WorkflowEngine DAG error: {e}")
            return WorkflowResult(
                workflow_id=workflow_id,
                workflow_name=workflow.name,
                success=False,
                steps=[],
                started_at=started_at,
                ended_at=ended_at,
                metadata={"error": str(e)},
            )

        completed: Dict[str, StepResult] = {}
        all_success = True

        # Build step lookup for branch resolution
        step_map: Dict[str, WorkflowStep] = {s.id: s for s in workflow.steps}

        for level in levels:
            # Skip levels where all steps are already completed (branch skipped them)
            if all(s.id in completed for s in level):
                continue

            # Check if any dep in this level is an approval step
            approval_step = next((s for s in level if s.step_type == "approval"), None)
            if approval_step:
                # Publish HITL notification and pause
                pause_result = self._pause_for_approval(
                    workflow_id, workflow, approval_step, completed, started_at
                )
                return pause_result

            # Skip level if a dependency of any step in this level failed
            if workflow.stop_on_failure:
                all_deps = {dep for s in level for dep in s.depends_on}
                dep_failed = any(
                    not completed[dep].success
                    for dep in all_deps
                    if dep in completed
                )
                if dep_failed:
                    logger.warning(f"WorkflowEngine: skipping level — dependency failed")
                    continue

            if len(level) == 1:
                # Serial — no overhead
                step = level[0]
                sr = self._run_step(step, completed)
                completed[step.id] = sr

                # ── Conditional branching ───────────────────────────────
                if sr.success and step.branch_on:
                    skipped = self._apply_branch(step, sr.output, step_map, completed)
                    if skipped:
                        completed.update(skipped)
                        logger.info(
                            f"WorkflowEngine: branch from '{step.id}' → "
                            f"skipped {list(skipped.keys())}"
                        )

                if not sr.success:
                    all_success = False
                    if workflow.stop_on_failure:
                        break
            else:
                # Parallel execution for independent steps
                level_success = self._run_level_parallel(level, completed, workflow.stop_on_failure)
                if not level_success:
                    all_success = False
                    if workflow.stop_on_failure:
                        break

        ended_at = datetime.utcnow().isoformat()

        result = WorkflowResult(
            workflow_id=workflow_id,
            workflow_name=workflow.name,
            success=all_success,
            steps=list(completed.values()),
            started_at=started_at,
            ended_at=ended_at,
        )

        self._persist(result)

        # ── Inbox notification for completion / failure ───────
        try:
            from store.inbox_store import publish_inbox_item
            _itype  = "workflow_completion" if all_success else "workflow_failed"
            _ititle = f"Workflow '{workflow.name}' {'completed' if all_success else 'FAILED'}"
            _ibody  = f"{len(completed)}/{len(workflow.steps)} steps completed. Started: {started_at}"
            publish_inbox_item(
                user_id="platform",
                type=_itype,
                title=_ititle,
                body=_ibody,
                source_id=workflow_id,
                metadata={"workflow_name": workflow.name, "success": all_success},
            )
        except Exception:
            pass

        logger.info(
            f"WorkflowEngine → END wf={workflow.name} "
            f"success={all_success} steps={len(completed)}"
        )
        return result

    # ========================================================
    # HITL: PAUSE + RESUME
    # ========================================================

    def _pause_for_approval(
        self,
        workflow_id: str,
        workflow: Workflow,
        approval_step: "WorkflowStep",
        completed: Dict[str, StepResult],
        started_at: str,
    ) -> WorkflowResult:
        """Persist workflow state and publish inbox notification."""
        # Serialise completed steps for later resume
        state_snapshot = {
            "workflow_id":   workflow_id,
            "workflow_name": workflow.name,
            "started_at":    started_at,
            "approval_step_id": approval_step.id,
            "completed": {
                sid: {
                    "step_id":    sr.step_id,
                    "step_name":  sr.step_name,
                    "success":    sr.success,
                    "output":     sr.output,
                    "error":      sr.error,
                    "started_at": sr.started_at,
                    "ended_at":   sr.ended_at,
                    "attempts":   sr.attempts,
                }
                for sid, sr in completed.items()
            },
        }

        # Persist to the workflow KV (fast primary path, DB=2)
        try:
            if self._memory and self._memory.ping():
                from core.config import RDB_WORKFLOW
                from core.kv import get_kv
                r = get_kv(RDB_WORKFLOW, decode_responses=True)
                r.setex(f"wf:paused:{workflow_id}", 86400 * 7, json.dumps(state_snapshot))
        except Exception as e:
            logger.warning(f"WorkflowEngine: failed to persist paused state to KV → {e}")

        # Persist to Postgres (durable fallback — survives Redis restart)
        self._upsert_run_record(workflow_id, workflow.name, "paused", state_snapshot)

        # Publish inbox notification
        try:
            from store.inbox_store import publish_inbox_item
            msg = approval_step.approval_message or f"Workflow '{workflow.name}' is waiting for your approval."
            publish_inbox_item(
                user_id=approval_step.notify_user or "team",
                item_type="workflow_approval",
                title=f"Approval Required — {workflow.name}",
                body=msg,
                data={
                    "workflow_id":      workflow_id,
                    "approval_step_id": approval_step.id,
                    "workflow_name":    workflow.name,
                },
            )
        except Exception as e:
            logger.warning(f"WorkflowEngine: inbox publish failed → {e}")

        logger.info(f"WorkflowEngine: PAUSED at step={approval_step.id} wf={workflow_id}")
        return WorkflowResult(
            workflow_id=workflow_id,
            workflow_name=workflow.name,
            success=False,
            steps=list(completed.values()),
            started_at=started_at,
            ended_at=datetime.utcnow().isoformat(),
            paused=True,
            approval_step_id=approval_step.id,
            approval_message=approval_step.approval_message,
            metadata={"paused_at": approval_step.id},
        )

    def resume(
        self,
        workflow_id: str,
        workflow: Workflow,
        approved: bool = True,
        feedback: str = "",
    ) -> WorkflowResult:
        """
        Resume a paused workflow after a HITL approval gate.

        approved=True  → continue from the step after the approval gate
        approved=False → mark the workflow FAILED and return immediately
        feedback       → injected as {approval_feedback} in subsequent steps
        """
        # Load state snapshot — KV first, then Postgres fallback
        snapshot: Optional[dict] = None
        try:
            from core.config import RDB_WORKFLOW
            from core.kv import get_kv
            r = get_kv(RDB_WORKFLOW, decode_responses=True)
            raw = r.get(f"wf:paused:{workflow_id}")
            if raw:
                snapshot = json.loads(raw)
                r.delete(f"wf:paused:{workflow_id}")
        except Exception as e:
            logger.warning(f"WorkflowEngine resume: KV load failed → {e}")

        if snapshot is None:
            # Redis miss — load from Postgres (handles Redis restart scenario)
            snapshot = self._load_run_snapshot(workflow_id)

        if not approved:
            logger.info(f"WorkflowEngine: REJECTED wf={workflow_id}")
            return WorkflowResult(
                workflow_id=workflow_id,
                workflow_name=workflow.name,
                success=False,
                steps=[],
                started_at=snapshot["started_at"] if snapshot else datetime.utcnow().isoformat(),
                ended_at=datetime.utcnow().isoformat(),
                metadata={"rejected": True, "feedback": feedback},
            )

        # Restore completed steps
        completed: Dict[str, StepResult] = {}
        if snapshot:
            for sid, sd in snapshot.get("completed", {}).items():
                completed[sid] = StepResult(
                    step_id=sd["step_id"],   step_name=sd["step_name"],
                    success=sd["success"],   output=sd["output"],
                    error=sd["error"],       started_at=sd["started_at"],
                    ended_at=sd["ended_at"], attempts=sd["attempts"],
                )

        # Inject approval feedback as virtual step output
        if feedback:
            completed["approval_feedback"] = StepResult(
                step_id="approval_feedback", step_name="Approval Feedback",
                success=True, output=feedback, error=None,
                started_at=datetime.utcnow().isoformat(),
                ended_at=datetime.utcnow().isoformat(),
            )

        approval_step_id = snapshot["approval_step_id"] if snapshot else None
        started_at       = snapshot["started_at"] if snapshot else datetime.utcnow().isoformat()

        # Re-run workflow from after the approval step
        levels   = self._group_by_level(workflow)
        past_approval = False
        all_success   = True

        for level in levels:
            if not past_approval:
                # Skip levels we already completed (or the approval step itself)
                step_ids = {s.id for s in level}
                if approval_step_id in step_ids or all(sid in completed for sid in step_ids):
                    if approval_step_id in step_ids:
                        past_approval = True
                    continue

            if len(level) == 1:
                sr = self._run_step(level[0], completed)
                completed[level[0].id] = sr
                if not sr.success:
                    all_success = False
                    if workflow.stop_on_failure:
                        break
            else:
                level_success = self._run_level_parallel(level, completed, workflow.stop_on_failure)
                if not level_success:
                    all_success = False
                    if workflow.stop_on_failure:
                        break

        ended_at = datetime.utcnow().isoformat()
        result = WorkflowResult(
            workflow_id=workflow_id,
            workflow_name=workflow.name,
            success=all_success,
            steps=list(completed.values()),
            started_at=started_at,
            ended_at=ended_at,
        )
        self._persist(result)
        logger.info(f"WorkflowEngine: RESUMED wf={workflow_id} success={all_success}")
        return result

    # ========================================================
    # PARALLEL LEVEL EXECUTION
    # ========================================================

    def _run_level_parallel(
        self,
        level: List["WorkflowStep"],
        completed: Dict[str, StepResult],
        stop_on_failure: bool,
    ) -> bool:
        """Execute all steps in a level concurrently. Returns True if all succeed."""
        level_results: Dict[str, StepResult] = {}
        all_ok = True

        with ThreadPoolExecutor(max_workers=min(len(level), PARALLEL_WORKERS)) as pool:
            # ARCH-F-019: contextvars (trace IDs, request IDs, structured
            # logging context) are not automatically inherited by threads
            # spawned from a ThreadPoolExecutor. Without copy_context(),
            # parallel step logs have no correlation IDs.
            _ctx = contextvars.copy_context()
            future_map = {
                pool.submit(_ctx.run, self._run_step, step, dict(completed)): step
                for step in level
            }
            for future in as_completed(future_map):
                step = future_map[future]
                try:
                    # _run_step already enforces step.timeout_sec internally;
                    # the outer result() call here has no additional timeout.
                    sr = future.result()
                except Exception as e:
                    sr = StepResult(
                        step_id=step.id, step_name=step.name,
                        success=False, output="", error=str(e),
                        started_at=datetime.utcnow().isoformat(),
                        ended_at=datetime.utcnow().isoformat(),
                    )
                level_results[step.id] = sr
                if not sr.success:
                    all_ok = False

        completed.update(level_results)
        return all_ok

    # ========================================================
    # STEP EXECUTION DISPATCHER
    # ========================================================

    def _run_step(
        self,
        step: WorkflowStep,
        completed: Dict[str, StepResult],
    ) -> StepResult:

        started_at = datetime.utcnow().isoformat()

        logger.info(
            f"WorkflowEngine: running step={step.id} type={step.step_type}"
        )

        # Resolve input — replace {step_id} with prior outputs
        resolved_input = self._resolve_input(step.input, completed)

        # PCI check on resolved input
        if not self._pci_check_input(resolved_input, step.id):
            return StepResult(
                step_id=step.id,
                step_name=step.name,
                success=False,
                output="",
                error="PCI compliance violation on step input",
                started_at=started_at,
                ended_at=datetime.utcnow().isoformat(),
            )

        # Dispatch — wrapped in per-step timeout
        def _dispatch():
            if step.step_type == "llm":
                return self._run_llm(resolved_input)
            elif step.step_type == "code":
                return self._run_code(resolved_input, "python", step.heal)
            elif step.step_type == "shell":
                return self._run_code(resolved_input, "bash", heal=False)
            elif step.step_type == "tool":
                return self._run_tool(step, resolved_input)
            elif step.step_type == "agent":
                return self._run_agent_step(step, resolved_input)
            elif step.step_type == "approval":
                return "APPROVAL_GATE", 0
            else:
                raise ValueError(f"Unknown step_type: {step.step_type!r}")

        try:
            with ThreadPoolExecutor(max_workers=1) as _pool:
                _future = _pool.submit(_dispatch)
                try:
                    output, attempts = _future.result(timeout=step.timeout_sec)
                except FutureTimeoutError:
                    _future.cancel()
                    raise TimeoutError(
                        f"Step '{step.id}' exceeded timeout of {step.timeout_sec}s"
                    )

        except Exception as e:
            logger.error(f"WorkflowEngine step {step.id} raised: {e}")
            return StepResult(
                step_id=step.id,
                step_name=step.name,
                success=False,
                output="",
                error=str(e),
                started_at=started_at,
                ended_at=datetime.utcnow().isoformat(),
            )

        ended_at = datetime.utcnow().isoformat()
        success  = output is not None and not str(output).startswith("Error")

        logger.info(
            f"WorkflowEngine: step={step.id} success={success}"
        )

        return StepResult(
            step_id=step.id,
            step_name=step.name,
            success=success,
            output=str(output) if output is not None else "",
            error=None if success else str(output),
            started_at=started_at,
            ended_at=ended_at,
            attempts=attempts,
        )

    # ========================================================
    # STEP TYPE HANDLERS
    # ========================================================

    def _run_llm(self, prompt: str):
        import time as _time
        from models.model_router import model_router

        _MAX_ATTEMPTS = 3
        last_exc: Exception = RuntimeError("LLM call failed after all retries")
        output = None
        attempts = 0

        for attempt in range(_MAX_ATTEMPTS):
            attempts = attempt + 1
            try:
                output = model_router.generate(prompt)
                break  # success — exit retry loop
            except Exception as _llm_exc:
                last_exc = _llm_exc
                if attempt < _MAX_ATTEMPTS - 1:
                    _backoff = 2 ** attempt  # 2s, then 4s
                    logger.warning(
                        f"WorkflowEngine._run_llm: attempt {attempts}/{_MAX_ATTEMPTS} failed "
                        f"({_llm_exc}), retrying in {_backoff}s"
                    )
                    _time.sleep(_backoff)
                else:
                    logger.error(
                        f"WorkflowEngine._run_llm: all {_MAX_ATTEMPTS} attempts failed — {_llm_exc}"
                    )
                    raise last_exc

        # ── Eval: workflow DAG step output (fire-and-forget) ──────────────────
        try:
            from core.evals import eval_engine as _ee
            _ee.eval_answer_quality(prompt[:400], output or "", [])
        except Exception:
            pass
        return output, attempts

    def _run_code(self, code: str, language: str, heal: bool):
        if heal:
            from sandbox.self_healing_engine import SelfHealingEngine
            engine = SelfHealingEngine()
            result = engine.execute_and_heal(code=code, language=language)
            output   = result.get("output") or result.get("error", "")
            attempts = result.get("attempts", 1)
            if not result.get("success"):
                # return error string so success detection works
                output = result.get("error", "Execution failed")
            return output, attempts
        else:
            from sandbox.docker_executor import docker_executor
            result   = docker_executor.execute(code=code, language=language)
            output   = result.get("output") or result.get("error", "")
            if not result.get("success"):
                output = result.get("output") or result.get("error", "Execution failed")
            return output, 1

    def _run_tool(self, step: WorkflowStep, resolved_input: str):
        if step.tool_fn is None:
            raise ValueError(f"Step {step.id!r} is type=tool but tool_fn is None")
        output = step.tool_fn(resolved_input)
        return str(output) if output is not None else "", 1

    def _run_agent_step(self, step: WorkflowStep, resolved_input: str):
        """
        Run a named Agent via AgentRunner.
        step.input holds the agent name; resolved_input is the message to send it.
        """
        agent_name = (step.input or "").strip()
        if not agent_name:
            raise ValueError(f"Step {step.id!r} is type=agent but no agent name set")
        try:
            from agents.agent_builder import AgentRunner
            runner = AgentRunner(agent_name)
            result = runner.run(resolved_input)
            answer = result.get("answer", "") if isinstance(result, dict) else str(result)
            return answer or "", 1
        except Exception as e:
            raise RuntimeError(f"Agent step '{agent_name}' failed: {e}") from e

    # ========================================================
    # INPUT TEMPLATING
    # ========================================================

    def _resolve_input(
        self,
        template: str,
        completed: Dict[str, StepResult],
    ) -> str:
        """Replace {step_id} placeholders with the output of that step."""
        resolved = template
        for step_id, result in completed.items():
            placeholder = f"{{{step_id}}}"
            if placeholder in resolved:
                resolved = resolved.replace(placeholder, result.output)
        return resolved

    # ========================================================
    # PCI CHECKS
    # ========================================================

    def _pci_check_input(self, text: str, step_id: str) -> bool:
        result = compliance_engine.validate_input(text)
        if result["blocked"]:
            logger.critical(
                f"WorkflowEngine: PCI blocked input for step={step_id}"
            )
            return False
        return True



    # ========================================================
    # CONDITIONAL BRANCHING
    # ========================================================

    def _apply_branch(
        self,
        step:      "WorkflowStep",
        output:    str,
        step_map:  Dict[str, "WorkflowStep"],
        completed: Dict[str, "StepResult"],
    ) -> Dict[str, "StepResult"]:
        """
        Check step.branch_on keywords against step output (case-insensitive).
        If a keyword matches, find the branch target step and mark all OTHER
        direct successors of this step as SKIPPED in the completed dict.

        Returns a dict of {step_id: StepResult(skipped)} to merge into completed.
        Returns {} if no branch keyword matched or nothing needs skipping.
        """
        if not step.branch_on:
            return {}

        output_lower = output.lower()

        # Find the first matching branch keyword
        target_step_id: Optional[str] = None
        matched_keyword: Optional[str] = None
        for keyword, branch_target in step.branch_on.items():
            if keyword.lower() in output_lower:
                target_step_id  = branch_target
                matched_keyword = keyword
                break

        if target_step_id is None:
            return {}   # no keyword matched — normal execution continues

        if target_step_id not in step_map:
            logger.warning(
                f"WorkflowEngine branch: step '{step.id}' branch_on target "
                f"'{target_step_id}' does not exist in workflow — ignoring branch"
            )
            return {}

        # Find all direct successors of this step (steps that depend only on step.id)
        successors = [
            s for s in step_map.values()
            if step.id in s.depends_on and s.id != target_step_id and s.id not in completed
        ]

        # Mark non-target successors as SKIPPED
        skipped: Dict[str, "StepResult"] = {}
        for s in successors:
            skipped[s.id] = StepResult(
                step_id    = s.id,
                step_name  = s.name,
                success    = True,
                output     = f"[SKIPPED: branched to '{target_step_id}' on keyword '{matched_keyword}']",
                error      = None,
                started_at = datetime.utcnow().isoformat(),
                ended_at   = datetime.utcnow().isoformat(),
            )

        return skipped

    # ========================================================
    # TOPOLOGICAL LEVEL GROUPING (Kahn's algorithm extended)
    # ========================================================

    def _group_by_level(self, workflow: Workflow) -> List[List["WorkflowStep"]]:
        """
        Return steps grouped by DAG level.
        Steps within the same level have no interdependencies and can
        run in parallel. Levels are ordered by dependency depth.
        """
        step_map = {s.id: s for s in workflow.steps}

        # Validate all dependency IDs exist
        for step in workflow.steps:
            for dep in step.depends_on:
                if dep not in step_map:
                    raise ValueError(
                        f"Step {step.id!r} depends on unknown step {dep!r}"
                    )

        # Build in-degree and adjacency
        in_degree: Dict[str, int] = {s.id: 0 for s in workflow.steps}
        dependents: Dict[str, List[str]] = defaultdict(list)

        for step in workflow.steps:
            for dep in step.depends_on:
                in_degree[step.id] += 1
                dependents[dep].append(step.id)

        # Kahn's: process level by level
        queue: deque = deque(s.id for s in workflow.steps if in_degree[s.id] == 0)
        levels: List[List["WorkflowStep"]] = []
        visited = 0

        while queue:
            level_size = len(queue)
            level: List["WorkflowStep"] = []
            for _ in range(level_size):
                sid = queue.popleft()
                level.append(step_map[sid])
                visited += 1
                for dep_sid in dependents[sid]:
                    in_degree[dep_sid] -= 1
                    if in_degree[dep_sid] == 0:
                        queue.append(dep_sid)
            levels.append(level)

        if visited != len(workflow.steps):
            raise ValueError("Workflow contains a dependency cycle")

        return levels

    # Keep backward-compatible alias for any external callers
    def _topological_sort(self, workflow: Workflow) -> List["WorkflowStep"]:
        """Flat ordered list (legacy). Prefer _group_by_level for new code."""
        return [step for level in self._group_by_level(workflow) for step in level]

    # ========================================================
    # MEMORY PERSISTENCE
    # ========================================================

    def _persist(self, result: WorkflowResult) -> None:
        # Redis memory (fast, transient)
        if self._memory is not None:
            try:
                steps_serialisable = [
                    {
                        "step_id":    s.step_id,
                        "step_name":  s.step_name,
                        "success":    s.success,
                        "output":     s.output[:500],
                        "error":      s.error,
                        "started_at": s.started_at,
                        "ended_at":   s.ended_at,
                        "attempts":   s.attempts,
                    }
                    for s in result.steps
                ]
                self._memory.save_workflow_run(
                    workflow_id=result.workflow_id,
                    workflow_name=result.workflow_name,
                    steps=steps_serialisable,
                    status="success" if result.success else "failed",
                    metadata=result.metadata,
                )
            except Exception as e:
                logger.warning(f"WorkflowEngine: Redis memory persist failed → {e}")

        # Postgres (durable)
        status = "completed" if result.success else "failed"
        self._upsert_run_record(result.workflow_id, result.workflow_name, status, None)

    # ========================================================
    # POSTGRES RUN STATE HELPERS
    # ========================================================

    def _upsert_run_record(
        self,
        workflow_id: str,
        workflow_name: str,
        status: str,
        snapshot: Optional[dict],
    ) -> None:
        """Write/update workflow_runs row in Postgres."""
        try:
            from db.database import SessionLocal
            from db.models import WorkflowRunRecord
            db = SessionLocal()
            try:
                record = db.query(WorkflowRunRecord).filter(
                    WorkflowRunRecord.workflow_id == workflow_id
                ).first()
                if record is None:
                    record = WorkflowRunRecord(
                        workflow_id=workflow_id,
                        workflow_name=workflow_name,
                        status=status,
                        state_json=snapshot,
                    )
                    db.add(record)
                else:
                    record.status     = status
                    record.state_json = snapshot
                    if status in ("completed", "failed"):
                        record.ended_at = datetime.utcnow()
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"WorkflowEngine: Postgres run persist failed → {e}")

    def _load_run_snapshot(self, workflow_id: str) -> Optional[dict]:
        """Load paused workflow snapshot from Postgres."""
        try:
            from db.database import SessionLocal
            from db.models import WorkflowRunRecord
            db = SessionLocal()
            try:
                record = db.query(WorkflowRunRecord).filter(
                    WorkflowRunRecord.workflow_id == workflow_id,
                    WorkflowRunRecord.status == "paused",
                ).first()
                if record and record.state_json:
                    logger.info(f"WorkflowEngine: loaded paused state from Postgres for {workflow_id}")
                    return record.state_json
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"WorkflowEngine: Postgres snapshot load failed → {e}")
        return None


# ============================================================
# SINGLETON
# ============================================================

workflow_engine = WorkflowEngine()
