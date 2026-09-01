# Jobs Router

## Overview

The **Jobs Router** (`routers/jobs_router.py`) is a FastAPI APIRouter that exposes
a REST interface for managing the platform's asynchronous job queue and workflow
planning lifecycle. It allows authenticated clients to:

- **Submit** background jobs to named queues (SDLC pipelines, durable workflows,
  chat jobs, memory maintenance, feedback loops, scheduled-workflow dispatch, etc.)
- **Inspect** individual job status or list recent jobs across all queues.
- **Cancel** queued or running jobs.
- **Query queue statistics** (queued / started / finished / failed counts).
- **Analyze workflow plans** — critical path, risk scores, estimated duration,
  and rollback order.
- **Resume interrupted workflows** from Redis-backed checkpoints.

All endpoints require authentication via `get_current_user` (see
[auth_dependencies](../auth_dependencies.md)). Job submission is further restricted
by a strict function-name allowlist to prevent arbitrary code execution.

---

## Architecture

```mermaid
flowchart LR
    Client["Client / Frontend / Internal Service"]

    subgraph Gateway["FastAPI Application"]
        Router["Jobs Router<br/>/jobs"]
        Auth["Auth Dependency<br/>get_current_user"]
    end

    subgraph Core["Core Services"]
        JobQueue["core.job_queue<br/>RQ + Redis"]
        Planner["workflows.planner<br/>PlanningEngine"]
        Engine["workflows.engine<br/>WorkflowStep"]
        Memory["memory.postgres_memory<br/>PostgresMemory"]
    end

    subgraph Infra["Infrastructure"]
        Redis[("Redis<br/>RQ Queues + Checkpoints")]
        Postgres[("Postgres<br/>workflow_history")]
    end

    subgraph Workers["Worker Pool"]
        W1["SDLC Workers"]
        W2["Durable Workflow Worker"]
        W3["Chat Worker"]
        W4["Memory / Feedback / Scheduler Workers"]
    end

    Client -->|HTTP /jobs| Router
    Router --> Auth
    Router -->|enqueue / cancel / status / list / stats| JobQueue
    JobQueue <--> Redis
    Redis -->|dequeues| Workers
    Workers --> W1
    Workers --> W2
    Workers --> W3
    Workers --> W4

    Router -->|get_workflow_plan| Memory
    Memory <--> Postgres
    Router -->|reconstruct steps| Engine
    Router -->|analyze_plan / resume_plan| Planner
    Planner <-->|checkpoint| Redis
```

The router is intentionally thin: it validates input, enforces security
constraints, and delegates all persistence and execution logic to
[core_job_queue](../core_job_queue.md), [workflows_planner](../workflows_planner.md),
and [memory_postgres_memory](../memory_postgres_memory.md).

---

## Module Dependencies

```mermaid
graph TD
    JobsRouter["routers/jobs_router.py"]

    JobsRouter -->|authentication| AuthDep["auth.dependencies<br/>get_current_user"]
    JobsRouter -->|enqueue / cancel / status / list / stats| JobQueue["core.job_queue"]
    JobsRouter -->|workflow run lookup| PostgresMem["memory.postgres_memory<br/>PostgresMemory"]
    JobsRouter -->|step reconstruction| WfEngine["workflows.engine<br/>WorkflowStep"]
    JobsRouter -->|plan analysis & resume| WfPlanner["workflows.planner<br/>PlanningEngine"]

    JobQueue -->|RQ| Redis[("Redis")]
    WfPlanner -->|checkpoint| Redis
    PostgresMem -->|workflow_history| Postgres[("Postgres")]

    click AuthDep href "auth_dependencies.md" "auth_dependencies"
    click JobQueue href "core_job_queue.md" "core_job_queue"
    click PostgresMem href "memory_postgres_memory.md" "memory_postgres_memory"
    click WfEngine href "workflows_engine.md" "workflows_engine"
    click WfPlanner href "workflows_planner.md" "workflows_planner"
```

### External Dependencies

| Dependency | Purpose | Documentation |
|---|---|---|
| `auth.dependencies.get_current_user` | Enforces authentication on every endpoint | [auth_dependencies](../auth_dependencies.md) |
| `core.job_queue` | RQ/Redis-backed enqueue, cancel, status, list, stats | [core_job_queue](../core_job_queue.md) |
| `memory.postgres_memory.PostgresMemory` | Loads workflow run history for plan analysis | [memory_postgres_memory](../memory_postgres_memory.md) |
| `workflows.engine.WorkflowStep` | Lightweight step object reconstructed for analysis | [workflows_engine](../workflows_engine.md) |
| `workflows.planner.PlanningEngine` | CPM analysis, risk scoring, checkpoint/resume | [workflows_planner](../workflows_planner.md) |

---

## API Endpoints

| Method | Path | Function | Description |
|---|---|---|---|
| `POST` | `/jobs` | `submit_job` | Submit a job to the queue; returns `job_id` |
| `DELETE` | `/jobs/{job_id}` | `cancel_job` | Cancel a queued or running job |
| `GET` | `/jobs/{job_id}` | `get_job` | Get status of a specific job |
| `GET` | `/jobs` | `list_jobs` | List recent jobs, optionally filtered by queue |
| `GET` | `/jobs/stats/queues` | `queue_stats` | Return job counts per queue |
| `GET` | `/jobs/workflows/{workflow_id}/plan` | `get_workflow_plan` | Critical-path analysis and risk scores |
| `POST` | `/jobs/workflows/{workflow_id}/resume` | `resume_workflow` | Resume an interrupted workflow from checkpoint |

---

## Core Components

### `JobSubmitRequest`

Pydantic model for job submission:

| Field | Type | Default | Description |
|---|---|---|---|
| `fn_name` | `str` | — | Dotted worker function path; **must** be in `_ALLOWED_FN_NAMES` |
| `payload` | `dict` | — | Arbitrary JSON payload passed to the worker function |
| `queue_name` | `Optional[str]` | `"default"` | Target RQ queue name |
| `timeout` | `Optional[int]` | `900` | Per-job wall-clock timeout in seconds (15 min default) |
| `retry_count` | `Optional[int]` | `2` | Number of automatic retries on failure |

### Security: Worker Function Allowlist

`_ALLOWED_FN_NAMES` is a `frozenset` that acts as a strict allowlist (SEC-02).
Only the following dotted function paths may be submitted via the API:

| `fn_name` | Worker Module |
|---|---|
| `workers.sdlc_worker.run_feature_pipeline_job` | [workers_sdlc_worker](../workers_sdlc_worker.md) |
| `workers.sdlc_worker.run_bug_pipeline_job` | [workers_sdlc_worker](../workers_sdlc_worker.md) |
| `workers.sdlc_worker.run_mr_review_job` | [workers_sdlc_worker](../workers_sdlc_worker.md) |
| `workers.sdlc_worker.run_mr_merge_job` | [workers_sdlc_worker](../workers_sdlc_worker.md) |
| `workers.sdlc_worker.run_reindex_job` | [workers_sdlc_worker](../workers_sdlc_worker.md) |
| `workers.durable_workflow_worker.execute_durable_workflow` | [workers_durable_workflow_worker](../workers_durable_workflow_worker.md) |
| `workers.chat_worker.run_chat_job` | [workers_chat_worker](../workers_chat_worker.md) |
| `workers.graph_worker.run_graph_index_job` | Knowledge graph indexing worker |
| `workers.memory_maintenance_worker.run_memory_maintenance` | [workers_memory_maintenance_worker](../workers_memory_maintenance_worker.md) |
| `workers.feedback_loop_worker.run_feedback_loop` | [workers_feedback_loop_worker](../workers_feedback_loop_worker.md) |
| `workers.workflow_scheduler_worker.dispatch_scheduled_workflows` | [workers_workflow_scheduler_worker](../workers_workflow_scheduler_worker.md) |

Any `fn_name` not in this set is rejected with **HTTP 400** before reaching the
queue, preventing arbitrary code execution through the job API.

---

## Data Flow

### Job Submission

```mermaid
sequenceDiagram
    participant C as Client
    participant R as Jobs Router
    participant A as Auth
    participant Q as core.job_queue
    participant Redis as Redis / RQ
    participant W as Worker Pool

    C->>R: POST /jobs {fn_name, payload, queue_name, timeout, retry_count}
    R->>A: get_current_user(token)
    A-->>R: user dict
    R->>R: Validate fn_name ∈ _ALLOWED_FN_NAMES
    alt Invalid fn_name
        R-->>C: 400 "not in permitted worker allowlist"
    end
    R->>Q: enqueue_job(fn_name, payload, queue_name, timeout, retry_count)
    Q->>Q: Back-pressure check (queue depth limit)
    Q->>Redis: q.enqueue(fn, payload, job_id, timeout, retry)
    Redis-->>Q: job_id
    Q-->>R: job_id
    R-->>C: {job_id, queue, status: "queued"}

    Note over Redis,W: Worker dequeues and executes asynchronously
    Redis->>W: dequeue job
    W->>W: Execute fn(payload)
```

### Job Inspection & Cancellation

```mermaid
sequenceDiagram
    participant C as Client
    participant R as Jobs Router
    participant Q as core.job_queue
    participant Redis as Redis / RQ

    C->>R: GET /jobs/{job_id}
    R->>Q: get_job_status(job_id)
    Q->>Redis: rq.job.Job.fetch(job_id)
    Redis-->>Q: status, result, error, timestamps
    Q-->>R: status dict
    alt status == "unknown"
        R-->>C: 404 "Job not found"
    else
        R-->>C: status dict
    end

    C->>R: DELETE /jobs/{job_id}
    R->>Q: cancel_job(job_id)
    Q->>Redis: job.cancel()
    Redis-->>Q: True / False
    alt not cancelled
        R-->>C: 404 "not found or already finished"
    else
        R-->>C: {job_id, cancelled: true}
    end
```

### Job Listing & Queue Stats

```mermaid
sequenceDiagram
    participant C as Client
    participant R as Jobs Router
    participant Q as core.job_queue
    participant Redis as Redis / RQ

    C->>R: GET /jobs?queue=sdlc&limit=50
    alt queue parameter provided
        R->>Q: list_jobs(queue_name, limit)
        Q->>Redis: q.job_ids[:limit]
        Redis-->>Q: job IDs
        Q->>Redis: fetch status per job
        Redis-->>Q: status dicts
    else no queue filter
        R->>Q: list_all_jobs(limit)
        Q->>Redis: iterate ALL_QUEUES + finished/failed registries
        Redis-->>Q: aggregated + sorted jobs
    end
    Q-->>R: job list
    R-->>C: {jobs: [...]}

    C->>R: GET /jobs/stats/queues
    R->>Q: queue_stats()
    Q->>Redis: count queued/started/finished/failed per queue
    Redis-->>Q: counts
    Q-->>R: stats dict
    R-->>C: {queue_name: {queued, started, finished, failed}, ...}
```

---

## Workflow Plan Analysis

The `get_workflow_plan` endpoint provides critical-path method (CPM) analysis
for a previously executed workflow. It does **not** execute the workflow; it
loads persisted history and runs `PlanningEngine.analyze_plan()`.

```mermaid
sequenceDiagram
    participant C as Client
    participant R as Jobs Router
    participant Mem as PostgresMemory
    participant PG as Postgres
    participant Engine as WorkflowStep
    participant Planner as PlanningEngine

    C->>R: GET /jobs/workflows/{workflow_id}/plan
    R->>Mem: get_workflow_run(workflow_id)
    Mem->>PG: SELECT * FROM workflow_history WHERE workflow_id = ...
    alt not found
        PG-->>Mem: no rows
        Mem-->>R: None
        R-->>C: 404 "Workflow not found"
    end
    PG-->>Mem: row with steps (JSON)
    Mem-->>R: workflow dict

    R->>R: Parse steps (JSON string → list[dict])
    loop each step dict
        R->>Engine: WorkflowStep(id, name, step_type, depends_on, timeout_sec)
    end

    R->>Planner: analyze_plan(steps)
    Planner->>Planner: Topological sort (Kahn's algorithm)
    Planner->>Planner: Forward pass: EST / EFT
    Planner->>Planner: Backward pass: LST / LFT
    Planner->>Planner: Compute slack → critical path
    Planner->>Planner: Estimate per-step risk
    Planner-->>R: PlanAnalysis

    R-->>C: {critical_path, total_risk, estimated_duration_sec, step_risks, step_slack, rollback_order}
```

### Plan Analysis Response

| Field | Type | Description |
|---|---|---|
| `workflow_id` | `str` | The workflow identifier |
| `critical_path` | `list[str]` | Step IDs where slack ≈ 0 |
| `total_risk` | `float` | Weighted average risk of critical-path steps (0.0–1.0) |
| `estimated_duration_sec` | `float` | Sum of `timeout_sec` on the critical path |
| `step_risks` | `dict[str, float]` | Per-step risk score (0.0–1.0) |
| `step_slack` | `dict[str, float]` | Per-step slack time in seconds |
| `rollback_order` | `list[str]` | Reverse topological order for rollback |

> For details on the CPM algorithm, risk heuristics, and `PlanAnalysis` internals,
> see [workflows_planner](../workflows_planner.md).

---

## Workflow Resume

The `resume_workflow` endpoint retrieves a Redis-backed checkpoint for an
interrupted workflow. The checkpoint contains completed steps and intermediate
results. The **caller** is responsible for re-enqueuing the workflow job with
the checkpoint data — this endpoint only returns the saved state.

```mermaid
sequenceDiagram
    participant C as Client
    participant R as Jobs Router
    participant Planner as PlanningEngine
    participant Redis as Redis

    C->>R: POST /jobs/workflows/{workflow_id}/resume
    R->>Planner: resume_plan(workflow_id)
    Planner->>Redis: GET plan_ckpt:{workflow_id}
    alt no checkpoint
        Redis-->>Planner: nil
        Planner-->>R: None
        R-->>C: 404 "No checkpoint found"
    else checkpoint exists
        Redis-->>Planner: JSON state
        Planner-->>R: checkpoint dict
    end
    R->>R: Compute checkpoint_age_sec
    R-->>C: {workflow_id, checkpoint, completed_steps, checkpoint_age_sec}
```

### Resume Response

| Field | Type | Description |
|---|---|---|
| `workflow_id` | `str` | The workflow identifier |
| `checkpoint` | `dict` | Full checkpoint state from Redis |
| `completed_steps` | `list[str]` | Step IDs already completed before interruption |
| `checkpoint_age_sec` | `float` | Seconds since the checkpoint was last written |

> Checkpoints are stored with a 24-hour TTL under the key `plan_ckpt:{workflow_id}`.
> See [workflows_planner](../workflows_planner.md) for checkpoint lifecycle details.

---

## Component Interaction Diagram

```mermaid
graph TD
    subgraph "Jobs Router Endpoints"
        Submit["submit_job<br/>POST /jobs"]
        Cancel["cancel_job<br/>DELETE /jobs/{id}"]
        Get["get_job<br/>GET /jobs/{id}"]
        List["list_jobs<br/>GET /jobs"]
        Stats["queue_stats<br/>GET /jobs/stats/queues"]
        Plan["get_workflow_plan<br/>GET /jobs/workflows/{id}/plan"]
        Resume["resume_workflow<br/>POST /jobs/workflows/{id}/resume"]
    end

    Auth["get_current_user<br/>(all endpoints)"]

    subgraph "core.job_queue"
        Enqueue["enqueue_job"]
        CancelFn["cancel_job"]
        StatusFn["get_job_status"]
        ListFn["list_jobs / list_all_jobs"]
        StatsFn["queue_stats"]
    end

    PostgresMem["PostgresMemory<br/>.get_workflow_run()"]
    Planner["PlanningEngine<br/>.analyze_plan() / .resume_plan()"]
    WorkflowStep["WorkflowStep<br/>(step reconstruction)"]

    Submit --> Auth
    Cancel --> Auth
    Get --> Auth
    List --> Auth
    Stats --> Auth
    Plan --> Auth
    Resume --> Auth

    Submit -->|validate allowlist| Submit
    Submit --> Enqueue
    Cancel --> CancelFn
    Get --> StatusFn
    List --> ListFn
    Stats --> StatsFn

    Plan --> PostgresMem
    Plan --> WorkflowStep
    Plan --> Planner

    Resume --> Planner
```

---

## Error Handling

| Scenario | HTTP Status | Detail |
|---|---|---|
| `fn_name` not in allowlist | `400` | `fn_name '...' is not in the permitted worker allowlist` |
| Job not found (cancel) | `404` | `Job {id} not found or already finished` |
| Job not found (get) | `404` | `Job {id} not found` |
| Workflow not found | `404` | `Workflow {id} not found` |
| No checkpoint for resume | `404` | `No checkpoint found for workflow {id}` |
| Plan analysis failure | `500` | `Plan analysis failed: {error}` |
| Resume failure | `500` | `Resume failed: {error}` |
| RQ/Redis unavailable (enqueue) | `500`* | Propagated `RuntimeError` from `core.job_queue` |

> *When `enqueue_job` detects that RQ is unavailable or the queue is at capacity,
> it raises a `RuntimeError`. The router does not catch this, so FastAPI returns
> a default `500` response. See [core_job_queue](../core_job_queue.md) for
> back-pressure and availability details.

---

## Design Decisions

### 1. Lazy Imports

All `core.job_queue` and `workflows.planner` imports are performed **inside**
the endpoint functions rather than at module top-level. This avoids circular
import issues and reduces startup cost for the FastAPI application.

### 2. Strict Allowlist (SEC-02)

The `_ALLOWED_FN_NAMES` frozenset is the sole security gate for job submission.
Adding a new worker function to the API requires updating this set — there is no
dynamic registration path. This prevents arbitrary code execution even if an
authenticated user crafts a malicious `fn_name`.

### 3. Thin Router Pattern

The router contains no business logic beyond input validation and response
shaping. All queue operations, persistence, and analysis are delegated to
[core_job_queue](../core_job_queue.md), [memory_postgres_memory](../memory_postgres_memory.md),
and [workflows_planner](../workflows_planner.md).

### 4. Caller-Driven Resume

The `resume_workflow` endpoint returns checkpoint state but does **not**
automatically re-enqueue the workflow. This gives the caller full control over
how the resumed workflow is dispatched (e.g., with modified payload, different
queue, or additional context).

---

## Relationship to Other Modules

```mermaid
graph LR
    JobsRouter["jobs_router"]

    JobsRouter -->|"delegates queue ops"| CoreJobQueue["core_job_queue"]
    JobsRouter -->|"loads workflow history"| PostgresMemory["memory_postgres_memory"]
    JobsRouter -->|"CPM analysis & checkpoints"| Planner["workflows_planner"]
    JobsRouter -->|"step model"| Engine["workflows_engine"]
    JobsRouter -->|"authenticates"| Auth["auth_dependencies"]

    CoreJobQueue -->|"dispatches to"| Workers["Worker Pool"]
    Workers --> SDLC["workers_sdlc_worker"]
    Workers --> Durable["workers_durable_workflow_worker"]
    Workers --> Chat["workers_chat_worker"]
    Workers --> Maint["workers_memory_maintenance_worker"]
    Workers --> Feedback["workers_feedback_loop_worker"]
    Workers --> Sched["workers_workflow_scheduler_worker"]

    click CoreJobQueue href "core_job_queue.md" "core_job_queue"
    click PostgresMemory href "memory_postgres_memory.md" "memory_postgres_memory"
    click Planner href "workflows_planner.md" "workflows_planner"
    click Engine href "workflows_engine.md" "workflows_engine"
    click Auth href "auth_dependencies.md" "auth_dependencies"
    click SDLC href "workers_sdlc_worker.md" "workers_sdlc_worker"
    click Durable href "workers_durable_workflow_worker.md" "workers_durable_workflow_worker"
    click Chat href "workers_chat_worker.md" "workers_chat_worker"
    click Maint href "workers_memory_maintenance_worker.md" "workers_memory_maintenance_worker"
    click Feedback href "workers_feedback_loop_worker.md" "workers_feedback_loop_worker"
    click Sched href "workers_workflow_scheduler_worker.md" "workers_workflow_scheduler_worker"
```

The Jobs Router is one of many routers in the `shared_api_routers` package. It
serves as the primary API surface for programmatic job management, complementing
higher-level routers like [sdlc_router](sdlc_router.md) and
[chat_router](chat_router.md) that enqueue jobs internally through
`core.job_queue` without exposing the raw queue API.
