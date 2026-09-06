# SDLC Pipeline Workers

## Overview

The **sdlc_pipeline_workers** module (`workers/sdlc_worker.py`) is the asynchronous execution layer for the AI-driven Software Development Life Cycle (SDLC) automation system. It provides a set of **RQ (Redis Queue) job functions** that wrap the core SDLC pipeline logic, translating queued payloads into calls against the pipeline agents, state machine, governance engine, and GitLab tooling.

Every function in this module is designed to be:

- **Importable by RQ workers** — each is a top-level callable with a serializable payload.
- **Idempotent & resumable** — terminal-state bail-outs, dedup-slot release, and suspend-not-fail semantics ensure safe retries.
- **Context-aware** — structured logging via `bind_context` / `clear_bound_context` with `correlation_id` and `pipeline_stage`.
- **Fail-safe** — exceptions update the run state to `FAILED` (or `SUSPENDED` for recoverable phases) and re-raise so RQ can retry; `SDLCCancelled` is caught gracefully.

The workers do **not** contain business logic themselves — they are thin orchestration wrappers that delegate to the [shared_core_sdlc_pipeline](../sdlc/shared_core_sdlc_pipeline.md) agents and the [store_layer](../storage/store_layer.md) persistence layer.

---

## Module Architecture

```mermaid
graph TB
    subgraph Triggers
        Router["sdlc_router<br/>(API endpoints)"]
        Webhook["GitLab/Jira Webhooks"]
        Scheduler["RQ Scheduler<br/>(cron)"]
    end

    subgraph Queue
        RQ["RQ Job Queue<br/>(Redis)"]
    end

    subgraph sdlc_pipeline_workers
        PrimaryJobs["Primary Pipeline Jobs<br/>run_feature_pipeline_job<br/>run_bug_pipeline_job<br/>run_pr_review_pipeline_job<br/>run_governance_pipeline_job"]
        ResumeJobs["HITL Resume Jobs<br/>resume_feature_job<br/>resume_bug_job<br/>resume_from_stage_job<br/>retry_commit_job<br/>run_pre_sm_resume_job"]
        GovJobs["Governance Jobs<br/>run_endgate_governance_job<br/>run_governance_review_job<br/>resume_in_pipeline_governance_job<br/>governance_author_fix_job<br/>governance_batch_fix_job"]
        Maintenance["Maintenance<br/>expire_stale_hitl_runs"]
    end

    subgraph Delegates
        Pipeline["agents.sdlc_pipeline<br/>run_feature_pipeline<br/>run_bug_pipeline<br/>resume_feature_after_design_approval<br/>run_governance_pipeline"]
        SM["agents.sdlc_state_machine<br/>CodingStateMachine"]
        GovEngine["agents.sdlc_governance<br/>engine + config"]
        CliEngine["agents.sdlc_cli_engine<br/>run_cli"]
    end

    subgraph Infrastructure
        Store["store.sdlc_store<br/>update_run_state / get_run"]
        Artifacts["store.sdlc_artifacts<br/>_store_artifact / _load_latest_artifact"]
        JobQueue["core.job_queue<br/>release_sdlc_slot / refresh_sdlc_slot"]
        GitLab["tools.gitlab_tools<br/>merge / diff / draft / notes"]
        Workspace["workers.workspace_sync_worker<br/>prepare_run_workspace"]
    end

    Router --> RQ
    Webhook --> RQ
    Scheduler --> RQ
    RQ --> PrimaryJobs
    RQ --> ResumeJobs
    RQ --> GovJobs
    Scheduler --> Maintenance

    PrimaryJobs --> Pipeline
    ResumeJobs --> Pipeline
    ResumeJobs --> SM
    GovJobs --> SM
    GovJobs --> GovEngine
    GovJobs --> CliEngine

    PrimaryJobs --> Store
    ResumeJobs --> Store
    GovJobs --> Store
    GovJobs --> Artifacts
    GovJobs --> GitLab
    GovJobs --> Workspace
    Maintenance --> Store
    Maintenance --> JobQueue
    PrimaryJobs --> JobQueue
```

---

## Core Responsibilities

| Responsibility | Description |
|---|---|
| **Pipeline execution** | Launch full feature, bug, PR-review, and governance-scan pipelines from queued jobs. |
| **HITL resume** | Resume pipelines after human-in-the-loop approval gates (design, solution, code, PR, governance). |
| **Stage-level resume** | Reconstruct the `CodingStateMachine` from durable artifacts and resume at a specific stage (pre-gate implement, post-gate apply, governance end-gate). |
| **Governance end-gate** | Run author-triggered governance scans over committed MR diffs, with bounded auto-fix loops and MR draft management. |
| **Commit retry & merge** | Replay the commit phase from durable artifacts; merge approved MRs. |
| **Stale HITL expiration** | Watchdog that expires runs past their approval deadline and renews dedup slots for active gates. |
| **Dedup slot lifecycle** | Release per-Jira-key and per-reporter concurrency slots after every pipeline outcome. |

---

## Component Reference

### Primary Pipeline Jobs

| Function | Payload | Delegates To | Description |
|---|---|---|---|
| `run_feature_pipeline_job` | `issue_dict` (Jira issue + `_run_id`) | `agents.sdlc_pipeline.run_feature_pipeline` | Full feature SDLC pipeline: preflight → baseline → normalize → classify → plan → implement → review → commit. |
| `run_bug_pipeline_job` | `issue_dict` | `agents.sdlc_pipeline.run_bug_pipeline` | Full bug-fix SDLC pipeline with solution-approval gate. |
| `run_pr_review_pipeline_job` | `pr_dict` (PR metadata + `_run_id`) | `agents.sdlc_pipeline.run_pr_review_pipeline` | PR review pipeline. Acquires a Redis lock (`pr_review:running:{run_id}`) to prevent double-execution with inline webhook threads. |
| `run_governance_pipeline_job` | `issue_dict` | `agents.sdlc_pipeline.run_governance_pipeline` | Standalone governance scan pipeline (not tied to a feature/bug run). |

### HITL Resume & Revision Jobs

| Function | Payload Keys | Delegates To | Description |
|---|---|---|---|
| `resume_feature_job` | `run_id`, `feedback` | `resume_feature_after_design_approval` | Resume feature pipeline after design approval. |
| `resume_bug_job` | `run_id`, `feedback` | `resume_bug_after_solution_approval` | Resume bug pipeline after solution approval. |
| `resume_feature_revision_job` | `run_id`, `feedback` | `run_feature_revision` | Run a requested feature revision cycle. |
| `resume_bug_revision_job` | `run_id`, `feedback` | `run_bug_revision` | Run a requested bug revision cycle. |
| `resume_pr_approval_job` | `run_id` | `resume_after_pr_approval` | Post-PR-approval cleanup: mark complete, notify. |
| `run_pre_sm_resume_job` | `run_id`, `start_at` | `resume_pre_sm_pipeline` | Re-entrant pre-state-machine resume (gate-reorder). Skips already-durable phases instead of restarting. |
| `resume_from_stage_job` | `run_id`, `target_stage`, `mode`, `feedback`, `actor`, `reason` | `CodingStateMachine` (reconstructed) | Generic stage resume. Rehydrates artifacts, reconstructs the SM, and routes to pre-gate implement, post-gate apply, or governance end-gate based on `target_stage`. |
| `retry_commit_job` | `run_id` | `CodingStateMachine.resume_commit` | Replay only the COMMITTING phase from durable `CODING`/`SLT` artifacts. Idempotent: branch reuse, create↔update flips, MR `_find_existing_mr`. |

### Governance Jobs

| Function | Payload Keys | Description |
|---|---|---|
| `run_endgate_governance_job` | `run_id`, `actor` | Author-triggered governance end-gate. Rehydrates the SM, reconciles the working branch, pushes local workspace to origin, re-drafts the MR, and runs `_run_governance_endgate` over the committed diff. |
| `run_governance_review_job` | `run_id` *or* `repo`+`ref`/`mr_iid`, `auto_fix`, `governance_skills`, `product_id` | Standalone governance review (report-first; optional bounded auto-fix). Two modes: **run_id mode** (diff from `VERIFIED_DIFF` artifact) and **repo mode** (diff from GitLab MR or fresh clone). Persists `GOVERNANCE_REPORT` artifact and standalone report file; posts MR note. |
| `resume_in_pipeline_governance_job` | `run_id`, `actor` | Resume a feature/bug run from `AWAITING_GOVERNANCE_APPROVAL` after all domains are approved. |
| `resume_governance_fix_job` | `run_id`, `actor` | Resume governance fix after all domains approved (standalone governance run). |
| `trigger_domain_fix_job` | `run_id`, `domain`, `actor`, `fix_instructions` | Run auto-fixer for one governance domain after author requests it. |
| `governance_author_fix_job` | `run_id`, `fingerprint`, `actor` | Bounded author remediation loop for a single finding (auto-fix + re-scan + convergence). |
| `governance_batch_fix_job` | `run_id`, `fingerprints`, `actor` | Bounded fixer session over a batch of findings the author explicitly asked to fix. |

### PR Comment & Merge Jobs

| Function | Payload Keys | Description |
|---|---|---|
| `address_pr_comments_job` | `run_id`, `repo`, `pr_number` | AI addresses reviewer comments on a PR via `address_pr_review_comments`. |
| `merge_pr_job` | `run_id`, `repo` (optional), `pr_number` (optional) | Merge an approved MR via `gitlab_merge_mr`. Looks up missing repo/PR from the run record. Transitions to `MERGED`. |

### Maintenance

| Function | Schedule | Description |
|---|---|---|
| `expire_stale_hitl_runs` | Every 15 min (RQ scheduler) | Scans all `AWAITING_*` runs. Expires those past `hitl_deadline` → `EXPIRED`. Renews Redis dedup/rate-limit slots for still-active gates via `refresh_sdlc_slot`. |

### Internal Helpers

| Function | Purpose |
|---|---|
| `_release_slot` | Releases the SDLC dedup + user-counter slot after any pipeline outcome. Compare-and-deletes by owner (`run_id` or job ID). |
| `_gov_bool` | Defensive bool coercion for JSONB-round-tripped values. |
| `_gov_resolve_and_set_gitlab_token` | Resolves and sets the per-thread GitLab token for standalone governance jobs. |
| `_gov_clone_workspace` | Materializes a throwaway workspace for standalone governance review/fix using `prepare_run_workspace`. |
| `_gov_diff_against_base` | `git diff` the current checkout against `origin/<base>`. Best-effort, never raises. |
| `_gov_push_fix` | Commit + push a governance fixer round's changes to the source branch. Best-effort. |
| `_push_local_workspace_to_origin` | Ensures the run's local workspace commits/edits are on `origin/<branch>` before the governance end-gate re-clones fresh. |

---

## Data Flow

### Feature / Bug Pipeline Job

```mermaid
sequenceDiagram
    participant RQ as RQ Worker
    participant Job as run_feature_pipeline_job
    participant Pipeline as agents.sdlc_pipeline
    participant Store as store.sdlc_store
    participant Slots as core.job_queue

    RQ->>Job: issue_dict {_run_id, key, repo, ...}
    Job->>Job: bind_context(correlation_id, pipeline_stage)
    Job->>Pipeline: run_feature_pipeline(issue_dict, run_id)

    alt Pipeline succeeds
        Pipeline-->>Job: return run_id
    else Pipeline fails
        Pipeline-->>Job: raises Exception
        Job->>Store: update_run_state(run_id, "FAILED", error)
        Job-->>RQ: re-raise (RQ retries)
    end

    Job->>Slots: _release_slot(issue_dict)
    Note over Slots: Compare-and-delete dedup key<br/>decrement per-reporter counter
```

### Stage-Level Resume (`resume_from_stage_job`)

This is the most complex worker function — it reconstructs the `CodingStateMachine` from durable artifacts and routes to different execution paths based on `target_stage`.

```mermaid
flowchart TD
    Start["resume_from_stage_job<br/>payload: run_id, target_stage, feedback"] --> LoadRun["get_run(run_id)<br/>load context, design, analysis"]
    LoadRun --> Reconstruct["Reconstruct CodingStateMachine<br/>from run context + artifacts"]
    Reconstruct --> Hydrate["Rehydrate in-memory state:<br/>code_output from VERIFIED_DIFF<br/>slt_output from SLT artifact<br/>risk_score from CLASSIFYING"]
    Hydrate --> ThreadGov["Thread governance flags<br/>run_governance_review<br/>governance_subset"]
    ThreadGov --> InjectFeedback["Inject resume_feedback<br/>into SM._resume_feedback"]
    InjectFeedback --> Branch{target_stage?}

    Branch -- "COMMITTING / TEST_VERIFY" --> PostGate["sm.mode = postgate<br/>sm.run()<br/>APPLYING → TEST_VERIFY → SLT → COMMITTING → MR"]
    Branch -- "GOVERNANCE_SCAN / GOVERNANCE_FIX / GOVERNANCE_REVERIFY" --> GovResume["sm.run_governance_review = True<br/>sm._run_governance_endgate()<br/>over committed diff"]
    Branch -- "IMPLEMENT / REVIEW / other" --> PreGate["sm.mode = pregate<br/>sm._phase_implement()<br/>re-capture VERIFIED_DIFF<br/>transition to approval gate"]

    PostGate --> Done["return completed"]
    GovResume --> Done
    PreGate --> GateCheck{Run state terminal?}
    GateCheck -- No --> TransitionGate["_transition to AWAITING_*_APPROVAL<br/>notify inbox + Teams"]
    GateCheck -- Yes --> Done
    TransitionGate --> Done

    style PostGate fill:#e1f5fe
    style GovResume fill:#fff3e0
    style PreGate fill:#e8f5e9
```

### Governance End-Gate Job

```mermaid
sequenceDiagram
    participant Author as Author (UI)
    participant Router as sdlc_router
    participant RQ as RQ Worker
    participant Job as run_endgate_governance_job
    participant SM as CodingStateMachine
    participant GitLab as tools.gitlab_tools
    participant Store as store.sdlc_store

    Author->>Router: POST /sdlc/runs/{id}/governance/start
    Router->>RQ: enqueue run_endgate_governance_job
    RQ->>Job: payload {run_id, actor}

    Job->>Store: get_run(run_id)
    Job->>Job: Reconstruct SM from context
    Job->>Job: Reconcile working_branch from COMMITTING artifact / MR
    Job->>Job: _push_local_workspace_to_origin(run_id, repo, branch)
    Job->>SM: _ensure_run_workspace(repo)
    Job->>GitLab: set_token(user PAT)
    Job->>GitLab: gitlab_set_mr_draft(gitlab_repo, pr_number, draft=True)
    Job->>SM: _run_governance_endgate(branch, pr_number, pr_url, commit_sha)

    alt Nothing blocking
        SM->>GitLab: un-draft MR
        SM->>Store: update_run_state → AWAITING_PR_APPROVAL
    else Blocking findings
        SM->>Store: seed per-domain approvals
        SM->>Store: update_run_state → AWAITING_GOVERNANCE_APPROVAL
        SM->>SM: _suspend("GOVERNANCE_SCAN")
    end

    alt Exception
        Job->>Store: update_run_state → SUSPENDED (GOVERNANCE_SCAN)
        Note over Job: Fail-closed: MR stays drafted,<br/>run stays resumable
    end
```

### Standalone Governance Review Job

```mermaid
flowchart TD
    Start["run_governance_review_job<br/>payload: run_id OR repo+ref/mr_iid"] --> ModeCheck{Mode?}

    ModeCheck -- "run_id" --> RunMode["Load run<br/>Read VERIFIED_DIFF artifact<br/>Rebuild unified diff via difflib<br/>Reuse live workspace if on disk"]
    ModeCheck -- "repo" --> RepoMode["Resolve GitLab token<br/>Get MR diff or clone workspace<br/>git diff against base branch"]

    RunMode --> AutoFixCheck{auto_fix & workspace?}
    RepoMode --> AutoFixCheck

    AutoFixCheck -- No --> Scan["run_governance_scan_snapshot<br/>(initial scan)"]
    AutoFixCheck -- Yes --> Scan

    Scan --> Blocking{Blocking findings?}
    Blocking -- No --> Report["Render report<br/>Persist GOVERNANCE_REPORT artifact<br/>Write standalone report file"]
    Blocking -- Yes --> FixLoop{auto_fix & workspace & iterations < max?}

    FixLoop -- Yes --> Fix["run_cli (code profile)<br/>governance fixer prompt"]
    Fix --> ReDiff["_gov_diff_against_base<br/>(re-derive diff from workspace)"]
    ReDiff --> Rescan["run_governance_scan_snapshot<br/>(rescan)"]
    Rescan --> Blocking
    FixLoop -- No / suspended --> Report

    Report --> Push{repo mode & pushed fix?}
    Push -- Yes --> BestEffortPush["_gov_push_fix (best-effort)"]
    Push -- No --> MRNote
    BestEffortPush --> MRNote{mr_iid & repo?}
    MRNote -- Yes --> PostNote["gitlab_post_governance_note"]
    MRNote -- No --> End["return verdict"]
    PostNote --> End

    style Scan fill:#e1f5fe
    style Fix fill:#fff3e0
    style Report fill:#e8f5e9
```

### Stale HITL Expiration

```mermaid
flowchart TD
    Start["expire_stale_hitl_runs<br/>(scheduled every 15 min)"] --> ListRuns["list_runs(limit=200)"]
    ListRuns --> Loop{For each run}
    Loop --> StateCheck{State in AWAITING_*?}
    StateCheck -- No --> Loop
    StateCheck -- Yes --> DeadlineCheck{Past hitl_deadline?}
    DeadlineCheck -- Yes --> Expire["update_run_state → EXPIRED<br/>error: approval window expired"]
    DeadlineCheck -- No --> Renew["refresh_sdlc_slot(jira_key, reporter)<br/>renew Redis dedup lease"]
    Expire --> Loop
    Renew --> Loop
    Loop --> Done["return expired count"]
```

---

## State Management & Error Handling

### Run State Transitions Driven by Workers

```mermaid
stateDiagram-v2
    [*] --> CREATED: enqueue job
    CREATED --> RUNNING: worker picks up
    RUNNING --> AWAITING_CODE_APPROVAL: pre-gate finalize
    RUNNING --> AWAITING_SOLUTION_APPROVAL: bug pre-gate finalize
    RUNNING --> AWAITING_PR_APPROVAL: MR created
    RUNNING --> AWAITING_GOVERNANCE_APPROVAL: governance end-gate blocking
    RUNNING --> SUSPENDED: recoverable error
    RUNNING --> COMMIT_FAILED: commit error (resumable)
    RUNNING --> MERGE_CONFLICT: conflict detected
    AWAITING_CODE_APPROVAL --> RUNNING: resume_feature_job
    AWAITING_SOLUTION_APPROVAL --> RUNNING: resume_bug_job
    AWAITING_GOVERNANCE_APPROVAL --> RUNNING: resume_in_pipeline_governance_job
    AWAITING_PR_APPROVAL --> MERGED: merge_pr_job
    COMMIT_FAILED --> AWAITING_PR_APPROVAL: retry_commit_job
    SUSPENDED --> RUNNING: resume_from_stage_job
    AWAITING_* --> EXPIRED: expire_stale_hitl_runs
    RUNNING --> FAILED: unrecoverable error
    MERGED --> [*]
    COMPLETE --> [*]
    FAILED --> [*]
    EXPIRED --> [*]
```

### Error Handling Patterns

| Pattern | Implementation | Example |
|---|---|---|
| **Suspend-not-fail** | Recoverable errors (commit, governance workspace) transition to `SUSPENDED` or `COMMIT_FAILED` instead of `FAILED`, keeping the run resumable. | `retry_commit_job` → `COMMIT_FAILED` on error |
| **Fail-closed governance** | Governance end-gate errors suspend at `GOVERNANCE_SCAN` with the MR staying drafted — never lets a change merge without sign-off. | `run_endgate_governance_job` exception handler |
| **Terminal-state bail** | Jobs that pick up a run already in a terminal state (`COMPLETE`, `MERGED`, `FAILED`, `CANCELLED`, `EXPIRED`) skip execution. | `retry_commit_job` terminal check |
| **SDLCCancelled** | Out-of-band cancellation raises `SDLCCancelled`, caught gracefully — state stays `CANCELLED`, no branch/MR created. | `resume_from_stage_job`, `run_governance_pipeline_job` |
| **Slot release** | `_release_slot` runs in `finally` blocks to guarantee dedup slots are freed regardless of outcome. | `run_feature_pipeline_job`, `run_bug_pipeline_job` |
| **Idempotent re-entry** | Branch reuse (409 → reuse), `gitlab_batch_commit` create↔update flips, `gitlab_create_mr` `_find_existing_mr`. | `retry_commit_job`, `merge_pr_job` |

---

## Dependencies

```mermaid
graph LR
    subgraph sdlc_pipeline_workers
        Worker["workers/sdlc_worker.py"]
    end

    Worker -->|"pipeline logic"| SharedCore["shared_core_sdlc_pipeline<br/>agents.sdlc_pipeline<br/>agents.sdlc_state_machine<br/>agents.sdlc_governance<br/>agents.sdlc_cli_engine"]
    Worker -->|"run state / events"| StoreLayer["store_layer<br/>store.sdlc_store<br/>store.sdlc_artifacts<br/>store.sdlc_governance_findings<br/>store.sdlc_governance_approvers"]
    Worker -->|"dedup slots"| CoreInfra["core_infrastructure<br/>core.job_queue<br/>core.logger<br/>core.config<br/>core.model_registry<br/>core.platform_credentials"]
    Worker -->|"GitLab API"| SharedInteg["shared_integrations<br/>tools.gitlab_tools"]
    Worker -->|"workspace clone"| ExtWorkers["external_integration_workers<br/>workers.workspace_sync_worker"]
    Worker -->|"DB session"| Database["database<br/>db.database"]
    Worker -->|"enqueue / API"| SdlcRouter["sdlc_router<br/>routers.sdlc_router"]
    Worker -->|"worker startup"| WorkerOrch["worker_orchestration<br/>workers.start_workers"]
```

### Key Dependency Details

| Dependency | Module Reference | Usage |
|---|---|---|
| `agents.sdlc_pipeline` | [shared_core_sdlc_pipeline](../sdlc/shared_core_sdlc_pipeline.md) | All pipeline entry points: `run_feature_pipeline`, `run_bug_pipeline`, `run_pr_review_pipeline`, `resume_*`, `run_governance_*`, `address_pr_review_comments`, `_resolve_gitlab_repo`, `_gov_resolve_gitlab_token`, `run_governance_scan_snapshot` |
| `agents.sdlc_state_machine.CodingStateMachine` | [shared_core_sdlc_pipeline](../sdlc/shared_core_sdlc_pipeline.md) | Reconstructed in `resume_from_stage_job`, `retry_commit_job`, `run_endgate_governance_job` for stage-level resume and governance end-gate execution |
| `agents.sdlc_governance` (engine, config) | [shared_core_sdlc_pipeline](../sdlc/shared_core_sdlc_pipeline.md) | Governance skill selection, fix-prompt building, report rendering, `max_iters()`, `parse_subset()` |
| `agents.sdlc_cli_engine` | [shared_core_sdlc_pipeline](../sdlc/shared_core_sdlc_pipeline.md) | `run_cli` for governance fixer loops in standalone review |
| `store.sdlc_store` | [store_layer](../storage/store_layer.md) | `get_run`, `update_run_state`, `add_run_event`, `list_runs`, `SDLCCancelled` |
| `store.sdlc_artifacts` | [store_layer](../storage/store_layer.md) | `_store_artifact`, `_load_latest_artifact`, `compute_input_hash` for `GOVERNANCE_REPORT` persistence |
| `core.job_queue` | [core_infrastructure](../core/core_infrastructure.md) | `release_sdlc_slot`, `refresh_sdlc_slot` for dedup/concurrency control |
| `tools.gitlab_tools` | [shared_integrations](../skills/shared_integrations.md) | `gitlab_merge_mr`, `gitlab_get_mr_diff`, `gitlab_set_mr_draft`, `gitlab_post_governance_note`, `gitlab_get_project_clone_url`, `set_token` |
| `workers.workspace_sync_worker` | [external_integration_workers](../workers/external_integration_workers.md) | `prepare_run_workspace` for governance workspace materialization |
| `db.database` | [database](../storage/database.md) | `SessionLocal` for governance product-ID resolution |

---

## Integration Points

### Upstream: How Jobs Are Enqueued

Jobs are enqueued by the [sdlc_router](sdlc_router.md) API endpoints and webhook handlers. The router validates payloads, pre-creates run records, acquires dedup slots, and enqueues the appropriate worker function via `core.job_queue.enqueue_sdlc_job` or `enqueue_hitl_resume_job`.

| Router Endpoint | Worker Function |
|---|---|
| `POST /sdlc/feature` (`_bg_feature`) | `run_feature_pipeline_job` |
| `POST /sdlc/bug` (`_bg_bug`) | `run_bug_pipeline_job` |
| `POST /sdlc/pr-review` (`_bg_pr_review`) | `run_pr_review_pipeline_job` |
| `POST /sdlc/runs/{id}/resume` | `resume_feature_job` / `resume_bug_job` / `resume_from_stage_job` |
| `POST /sdlc/runs/{id}/retry-commit` | `retry_commit_job` |
| `POST /sdlc/runs/{id}/governance/start` | `run_endgate_governance_job` |
| `POST /sdlc/governance/review` | `run_governance_review_job` |
| `POST /sdlc/runs/{id}/governance/resume` | `resume_in_pipeline_governance_job` / `resume_governance_fix_job` |
| `POST /sdlc/runs/{id}/governance/domain-fix` | `trigger_domain_fix_job` |
| `POST /sdlc/runs/{id}/governance/author-fix` | `governance_author_fix_job` |
| `POST /sdlc/runs/{id}/governance/run-fixes` | `governance_batch_fix_job` |
| `POST /sdlc/runs/{id}/merge` | `merge_pr_job` |
| `POST /sdlc/runs/{id}/address-comments` | `address_pr_comments_job` |

### Downstream: Worker Orchestration

Worker processes are started by [worker_orchestration](../workers/worker_orchestration.md) (`workers/start_workers.py`), which spawns RQ worker processes that consume the SDLC queue. The `expire_stale_hitl_runs` function is registered as a scheduled cron job within the same orchestration layer.

### Event Consumption

SDLC state-change events produced by `store.sdlc_store.update_run_state` are consumed by the [kafka_event_consumer](../workers/kafka_event_consumer.md) (`_handle_sdlc_events`) for audit trails and secondary writes.

---

## Key Design Patterns

### 1. Thin Wrapper / Delegate Pattern

Every worker function follows the same structure:

```
bind_context → delegate to agents.sdlc_pipeline → catch SDLCCancelled → catch Exception (update_run_state FAILED) → finally (_release_slot)
```

This keeps business logic in the agent layer and makes workers testable in isolation.

### 2. Artifact-Driven Resume

The `CodingStateMachine` is **stateless across process boundaries** — all state is persisted as stage artifacts (`VERIFIED_DIFF`, `SLT`, `CLASSIFYING`, `COMMITTING`, `GOVERNANCE_REPORT`). Resume jobs reconstruct the SM and rehydrate in-memory fields from these artifacts:

- `code_output` ← derived from `VERIFIED_DIFF` edits
- `slt_output` ← `SLT` artifact
- `_risk_score` ← `CLASSIFYING` artifact
- Governance flags ← run context (`run_governance_review`, `governance_skills`)

### 3. Report-First Governance

`run_governance_review_job` never forces a pipeline suspend — it generates a report first, then optionally runs a bounded auto-fix loop. This separates governance *observation* from governance *enforcement* (which lives in `CodingStateMachine._run_governance_endgate`).

### 4. Per-User Credential Isolation

All GitLab operations use the triggering user's PAT via thread-local `set_token`, never mutating the global `GITLAB_TOKEN` env var. This is resolved via `core.platform_credentials.get_gitlab_token` and applied at the start of governance and commit jobs.

### 5. Empty-Diff Guard

The governance end-gate includes an explicit empty-diff guard: if the freshly-cloned working branch has no changes over its base (typically due to unpushed local commits), the job suspends with an actionable message rather than producing a false-green scan.

---

## References

- [shared_core_sdlc_pipeline](../sdlc/shared_core_sdlc_pipeline.md) — Core SDLC pipeline agents, state machine, governance engine, and CLI engine
- [store_layer](../storage/store_layer.md) — Run state persistence, stage artifacts, governance findings/approvers
- [core_infrastructure](../core/core_infrastructure.md) — Job queue, logging, config, model registry, platform credentials
- [shared_integrations](../skills/shared_integrations.md) — GitLab tools, Jira tools, and other connector adapters
- [external_integration_workers](../workers/external_integration_workers.md) — Workspace sync worker for per-run git clone management
- [database](../storage/database.md) — SQLAlchemy session management and SDLC run models
- [sdlc_router](sdlc_router.md) — API endpoints that enqueue these worker jobs
- [worker_orchestration](../workers/worker_orchestration.md) — RQ worker process startup and cron scheduling
- [kafka_event_consumer](../workers/kafka_event_consumer.md) — Consumes SDLC state-change events for audit
