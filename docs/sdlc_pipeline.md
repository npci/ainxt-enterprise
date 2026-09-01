# sdlc_pipeline Module Overview

## Purpose

The `sdlc_pipeline` module is the frontend React implementation of the Software Development Lifecycle (SDLC) pipeline interface in the `ai-ui` application. It provides an interactive dashboard for orchestrating, monitoring, and governing AI-driven software development workflows — from feature/bug initiation through design, coding, testing, review, and merge.

The module renders pipeline runs as cards, exposes per-run artifact inspection, supports human-in-the-loop governance actions (approve, reject, request changes, retry), and integrates manifest validation, security findings, and cross-model review outputs into a unified user experience.

---

## Architecture

The module is organized around a single root component, `SDLCPipeline`, that composes several focused sub-systems:

```mermaid
flowchart TB
    subgraph User["User"]
        U[Triggers / Approves / Rejects]
    end

    subgraph SDLCPipelineModule["sdlc_pipeline module"]
        direction TB
        Core[Pipeline Core<br/>SDLCPipeline, RunCard, RunDetail, EventLog]
        Artifacts[Artifact Views<br/>Context, Outputs, Design, Coding, Testing, Review, Commit]
        Governance[Governance Actions<br/>Manifest banner, Resume, Run governance, Send to governance]
        Approval[Approval Actions<br/>Approve, Reject, Retry, Request changes, Export report]
        Trigger[Trigger Modal<br/>Start feature / bug / PR review]
        Layout[Layout Helpers<br/>Section, Row]
    end

    U --> Trigger
    Trigger --> Core
    Core --> Artifacts
    Core --> Governance
    Core --> Approval
    Core --> Layout
    Governance --> Approval
    Artifacts --> Governance
```

### Component Hierarchy

```mermaid
classDiagram
    class SDLCPipeline {
        +render()
        +handleTriggered()
        +verifyChain()
    }
    class RunCard {
        +run summary
    }
    class RunDetail {
        +selected run
        +EventLog
    }
    class ArtifactViews {
        +ContextTab
        +OutputsTab
        +DesignArtifact
        +CodingArtifact
        +TestingArtifact
        +ReviewArtifact
        +CommitArtifact
    }
    class GovernanceActions {
        +ManifestValidationBanner
        +GovernanceResumePanel
        +runGovernanceNow()
        +doSendToGovernance()
    }
    class ApprovalActions {
        +ApprovalPanel
        +BaselineActionPanel
        +StageActionPanel
        +doApprove()
        +doReject()
        +doRetry()
    }
    class TriggerModal {
        +start pipeline
    }
    class LayoutHelpers {
        +Section
        +Row
    }

    SDLCPipeline --> RunCard
    SDLCPipeline --> RunDetail
    SDLCPipeline --> ArtifactViews
    SDLCPipeline --> GovernanceActions
    SDLCPipeline --> ApprovalActions
    SDLCPipeline --> TriggerModal
    SDLCPipeline --> LayoutHelpers
    RunDetail --> ArtifactViews
    RunDetail --> GovernanceActions
    RunDetail --> ApprovalActions
```

### Data Flow

```mermaid
sequenceDiagram
    participant User
    participant SDLCPipeline
    participant TriggerModal
    participant Backend
    participant RunDetail
    participant ArtifactViews
    participant GovernanceActions
    participant ApprovalActions

    User->>TriggerModal: select pipeline type / fill inputs
    TriggerModal->>Backend: POST trigger feature/bug/PR review
    Backend-->>SDLCPipeline: new run created
    SDLCPipeline->>RunDetail: select run
    RunDetail->>ArtifactViews: load stage artifacts
    ArtifactViews-->>RunDetail: render design/coding/testing/review/commit
    RunDetail->>GovernanceActions: evaluate governance state
    GovernanceActions->>Backend: run governance / resume / send to governance
    Backend-->>GovernanceActions: findings / verdict
    GovernanceActions->>ApprovalActions: surface approval decisions
    User->>ApprovalActions: approve / reject / request changes / retry
    ApprovalActions->>Backend: submit decision
    Backend-->>SDLCPipeline: updated run status
```

---

## Core Components Documentation

| Sub-module | Responsibility | Key Components |
|------------|----------------|----------------|
| [pipeline_core](sdlc_pipeline/pipeline_core.md) | Main orchestration, run listing, run selection, event log, and chain verification | `SDLCPipeline`, `RunCard`, `RunDetail`, `EventLog`, `verifyChain`, `handleTriggered` |
| [artifact_views](sdlc_pipeline/artifact_views.md) | Render stage-specific outputs and metadata for each pipeline phase | `ContextTab`, `OutputsTab`, `DesignArtifact`, `CodingArtifact`, `TestingArtifact`, `ReviewArtifact`, `CommitArtifact`, `AnalyzeArtifact`, `CrossModelReviewArtifact`, `DesignScopeView`, `ReviewVerdictView` |
| [governance_actions](sdlc_pipeline/governance_actions.md) | Trigger, resume, and monitor governance scans and findings | `ManifestValidationBanner`, `GovernanceResumePanel`, `resumeGovernance`, `runGovernanceNow`, `doSendToGovernance`, `handleApprovalDone` |
| [approval_actions](sdlc_pipeline/approval_actions.md) | Human-in-the-loop decisions and run control | `ApprovalPanel`, `BaselineActionPanel`, `StageActionPanel`, `doApprove`, `doReject`, `doCancel`, `doRetry`, `doRequestChanges`, `RetryCommitButton`, `handleSubmit`, `exportReport` |
| [trigger_modal](sdlc_pipeline/trigger_modal.md) | UI for initiating new pipeline runs | `TriggerModal` |
| [layout_helpers](sdlc_pipeline/layout_helpers.md) | Reusable presentational primitives for consistent metadata layout | `Section`, `Row` |

---

## Integration Notes

- The module communicates with backend SDLC APIs (e.g., `routers/sdlc_router.py`) to trigger runs, fetch artifacts, submit governance decisions, and resume/retry stages.
- It consumes status models and signal components from sibling `sdlc_*` modules such as [`sdlc_status_model`](sdlc_status_model.md), [`sdlc_gate_signal`](sdlc_gate_signal.md), [`sdlc_governance_review`](sdlc_governance_review.md), [`sdlc_planning_artifact`](sdlc_planning_artifact.md), and [`sdlc_pipeline_stepper`](sdlc_pipeline_stepper.md).
- Backend execution is handled by [`workers/sdlc_pipeline_workers`](../workers/sdlc_pipeline_workers.md), which processes feature, bug, PR review, governance, and resume jobs asynchronously.