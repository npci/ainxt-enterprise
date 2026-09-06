# Slack Router

## Overview

The **Slack Router** (`routers/slack_router.py`) is a FastAPI `APIRouter` that
serves as the inbound webhook bridge between the **Slack platform** and the
ABStudio / AiNxt backend. It exposes two endpoints under the `/slack` prefix:

| Endpoint | Method | Purpose |
|---|---|---|
| `/slack/events` | POST | Receives Slack Events API payloads (URL verification, `app_mention`). |
| `/slack/interactions` | POST | Receives Slack interactive component payloads (button clicks for HITL approval/rejection). |

The router is intentionally thin: it validates Slack signatures, parses
payloads, dispatches work to the appropriate subsystem (agent job queue or SDLC
pipeline resume), and sends acknowledgement messages back to the Slack channel.
It does **not** contain business logic for agent execution or SDLC state
management — those responsibilities are delegated to dedicated modules.

---

## Architecture

```mermaid
graph TB
  subgraph Slack["Slack Platform"]
    SlackAPI["Slack API / Events API"]
    SlackInteract["Slack Interactive Components"]
  end

  subgraph Gateway["API Gateway / FastAPI App"]
    Router["Slack Router<br/>routers/slack_router.py"]
  end

  subgraph Core["Core Infrastructure"]
    SlackBot["core.slack_bot<br/>verify_slack_signature<br/>send_agent_response"]
    Logger["core.logger"]
    JobQueue["core.job_queue<br/>enqueue_agent_job"]
  end

  subgraph SDLC["SDLC Subsystem"]
    SdlcStore["store.sdlc_store<br/>get_run / update_run_state / add_run_event"]
    SdlcPipeline["agents.sdlc_pipeline<br/>resume_feature_after_design_approval<br/>resume_bug_after_solution_approval<br/>resume_after_pr_approval"]
  end

  subgraph Workers["Worker Layer"]
    AgentWorker["workers.agent_worker<br/>run_agent_job"]
  end

  SlackAPI -->|"POST /slack/events"| Router
  SlackInteract -->|"POST /slack/interactions"| Router
  Router -->|"signature verify / send message"| SlackBot
  Router -->|"log"| Logger
  Router -->|"enqueue agent job"| JobQueue
  JobQueue -->|"dispatch"| AgentWorker
  Router -->|"HITL state read/write"| SdlcStore
  Router -->|"resume pipeline (background thread)"| SdlcPipeline
  SlackBot -->|"chat.postMessage"| SlackAPI
```

### Module Boundaries

The router sits at the **edge** of the system. All inbound Slack traffic flows
through it, but it delegates immediately to:

- **`core.slack_bot`** — HMAC-SHA256 signature verification and outbound Slack
  messaging (see [core_infrastructure.md](../core/core_infrastructure.md)).
- **`core.job_queue`** — asynchronous agent job dispatch (see
  [core_infrastructure.md](../core/core_infrastructure.md)).
- **`store.sdlc_store`** — SDLC run state persistence and audit trail (see
  [store_layer.md](../storage/store_layer.md)).
- **`agents.sdlc_pipeline`** — SDLC pipeline resume functions after Human-in-the-
  Loop (HITL) approvals (see
  [shared_core_sdlc_pipeline.md](../sdlc/shared_core_sdlc_pipeline.md)).

> **Note:** Outbound Slack connector actions (reading channels, posting rich
> messages, etc.) are handled by the `SlackAdapter` in the connectors layer —
> see [connectors_integrations.md](../connectors/connectors_integrations.md). This router only
> handles inbound webhook callbacks.

---

## Dependencies

```mermaid
graph LR
  Router["routers/slack_router.py"]

  Router -->|"verify_slack_signature()"| SlackBot["core.slack_bot"]
  Router -->|"send_agent_response()"| SlackBot
  Router -->|"enqueue_agent_job()"| JobQueue["core.job_queue"]
  Router -->|"get_run()"| SdlcStore["store.sdlc_store"]
  Router -->|"update_run_state()"| SdlcStore
  Router -->|"add_run_event()"| SdlcStore
  Router -->|"resume_feature_after_design_approval()"| SdlcPipeline["agents.sdlc_pipeline"]
  Router -->|"resume_bug_after_solution_approval()"| SdlcPipeline
  Router -->|"resume_after_pr_approval()"| SdlcPipeline
  Router -->|"logger"| CoreLogger["core.logger"]
```

| Dependency | Import | Role |
|---|---|---|
| `core.slack_bot` | `verify_slack_signature`, `send_agent_response` | Request authentication; posting replies to Slack channels. |
| `core.job_queue` | `enqueue_agent_job` | Enqueues an agent run onto the `Q_AGENT` queue for `workers.agent_worker`. |
| `store.sdlc_store` | `get_run`, `update_run_state`, `add_run_event` | Reads SDLC run state, transitions state, and appends signed audit events. |
| `agents.sdlc_pipeline` | `resume_feature_after_design_approval`, `resume_bug_after_solution_approval`, `resume_after_pr_approval` | Resumes the SDLC coding/PR pipeline after a human approves via Slack. |
| `core.logger` | `logger` | Structured logging. |

---

## Endpoints

### POST `/slack/events`

Receives Slack Events API payloads.

**Headers consumed:**

| Header | Description |
|---|---|
| `X-Slack-Request-Timestamp` | Unix timestamp used in signature base string and staleness check. |
| `X-Slack-Signature` | `v0=`-prefixed HMAC-SHA256 signature to verify. |

**Handled event types:**

| Payload `type` / `event.type` | Behaviour |
|---|---|
| `url_verification` (top-level) | Returns `{"challenge": "..."}` to complete the Slack app setup handshake. |
| `app_mention` | Strips the bot mention (`<@BOT_ID>`), enqueues an agent job for `sdlc-coding-agent`, and posts an acknowledgement to the channel. |
| *any other* | Returns `{"ok": true}` (acknowledged, no action). |

**Security:** Every request is signature-verified before parsing. If
`SLACK_SIGNING_SECRET` is not configured in `core.slack_bot`, verification
returns `True` (open mode — suitable only for local development). Requests with
timestamps older than 5 minutes are rejected.

### POST `/slack/interactions`

Receives Slack interactive component payloads (button clicks). Unlike the events
endpoint, the payload arrives as **form-encoded** data with a `payload` field
containing a JSON string.

**Handled action IDs:**

| Action ID prefix | Action value | Behaviour |
|---|---|---|
| `hitl_approve_` | SDLC `run_id` | Approves the SDLC run and resumes the pipeline. |
| `hitl_reject_` | SDLC `run_id` | Rejects the SDLC run and marks it `FAILED`. |

---

## Component Interaction

```mermaid
graph TB
  subgraph Router["slack_router.py"]
    Events["slack_events()"]
    Interactions["slack_interactions()"]
    Approve["_handle_hitl_approve()"]
    Reject["_handle_hitl_reject()"]
  end

  Events -->|"app_mention"| Enqueue["enqueue_agent_job()"]
  Events -->|"ack message"| Send["send_agent_response()"]

  Interactions -->|"hitl_approve_"| Approve
  Interactions -->|"hitl_reject_"| Reject

  Approve -->|"get_run()"| GetRun["store.sdlc_store"]
  Approve -->|"add_run_event()"| AddEvent["store.sdlc_store"]
  Approve -->|"update_run_state(APPROVED)"| UpdateState["store.sdlc_store"]
  Approve -->|"background thread"| Resume["agents.sdlc_pipeline"]
  Approve -->|"confirmation"| Send

  Reject -->|"get_run()"| GetRun
  Reject -->|"add_run_event(FAILED)"| AddEvent
  Reject -->|"update_run_state(FAILED)"| UpdateState
  Reject -->|"rejection notice"| Send
```

---

## Process Flow: `app_mention` Event

```mermaid
sequenceDiagram
  participant Slack as Slack Platform
  participant Router as slack_events()
  participant Bot as core.slack_bot
  participant Queue as core.job_queue
  participant Worker as workers.agent_worker

  Slack->>Router: POST /slack/events (app_mention)
  Router->>Bot: verify_slack_signature(body, timestamp, sig)
  Bot-->>Router: True

  Router->>Router: Parse JSON, extract event
  Router->>Router: Strip bot mention from text
  Router->>Queue: enqueue_agent_job("sdlc-coding-agent", message)
  Queue-->>Router: job_id

  par Background thread
    Queue->>Worker: dispatch run_agent_job
  end

  Router->>Bot: send_agent_response(channel, ack message)
  Bot->>Slack: chat.postMessage
  Router-->>Slack: {"ok": true}
```

---

## Process Flow: HITL Approve

```mermaid
sequenceDiagram
  participant Slack as Slack Platform
  participant Router as slack_interactions()
  participant Helper as _handle_hitl_approve()
  participant Store as store.sdlc_store
  participant Pipeline as agents.sdlc_pipeline
  participant Bot as core.slack_bot

  Slack->>Router: POST /slack/interactions (hitl_approve_{run_id})
  Router->>Bot: verify_slack_signature(body, timestamp, sig)
  Bot-->>Router: True

  Router->>Router: Parse form-encoded payload
  Router->>Helper: _handle_hitl_approve(run_id, user, channel)

  Helper->>Store: get_run(run_id)
  Store-->>Helper: run dict (state)

  alt State in allowed HITL states
    Helper->>Store: add_run_event(run_id, state, "APPROVED", actor=user)
    Helper->>Store: update_run_state(run_id, "APPROVED", context_patch)
    par Background thread
      Helper->>Pipeline: resume_feature_after_design_approval / resume_bug_after_solution_approval / resume_after_pr_approval
    end
    Helper->>Bot: send_agent_response(channel, "✅ approved, resuming...")
  else State not applicable
    Helper->>Bot: send_agent_response(channel, "approval not applicable")
  end

  Bot->>Slack: chat.postMessage
  Router-->>Slack: {"ok": true}
```

### Allowed HITL States for Approval

| Run State | Resume Function Called |
|---|---|
| `AWAITING_DESIGN_APPROVAL` | `resume_feature_after_design_approval(run_id, "")` |
| `AWAITING_SOLUTION_APPROVAL` | `resume_bug_after_solution_approval(run_id, "")` |
| `AWAITING_PR_APPROVAL` | `resume_after_pr_approval(run_id)` |

If the run is in any other state, approval is rejected with a channel message
and no state transition occurs.

---

## Process Flow: HITL Reject

```mermaid
sequenceDiagram
  participant Slack as Slack Platform
  participant Router as slack_interactions()
  participant Helper as _handle_hitl_reject()
  participant Store as store.sdlc_store
  participant Bot as core.slack_bot

  Slack->>Router: POST /slack/interactions (hitl_reject_{run_id})
  Router->>Bot: verify_slack_signature(body, timestamp, sig)
  Bot-->>Router: True

  Router->>Router: Parse form-encoded payload
  Router->>Helper: _handle_hitl_reject(run_id, user, channel)

  Helper->>Store: get_run(run_id)
  Store-->>Helper: run dict

  Helper->>Store: add_run_event(run_id, state, "FAILED", actor=user, output=reason)
  Helper->>Store: update_run_state(run_id, "FAILED", error=reason, context_patch)
  Helper->>Bot: send_agent_response(channel, "❌ rejected")
  Bot->>Slack: chat.postMessage
  Router-->>Slack: {"ok": true}
```

---

## HITL State Transitions

```mermaid
stateDiagram-v2
  [*] --> AWAITING_DESIGN_APPROVAL: Feature pipeline pauses
  [*] --> AWAITING_SOLUTION_APPROVAL: Bug pipeline pauses
  [*] --> AWAITING_PR_APPROVAL: PR ready for review

  AWAITING_DESIGN_APPROVAL --> APPROVED: Slack approve button
  AWAITING_SOLUTION_APPROVAL --> APPROVED: Slack approve button
  AWAITING_PR_APPROVAL --> APPROVED: Slack approve button

  AWAITING_DESIGN_APPROVAL --> FAILED: Slack reject button
  AWAITING_SOLUTION_APPROVAL --> FAILED: Slack reject button
  AWAITING_PR_APPROVAL --> FAILED: Slack reject button

  APPROVED --> [*]: Pipeline resumes (background thread)
  FAILED --> [*]: Terminal state
```

> The `update_run_state()` function in `store.sdlc_store` performs a
> synchronous Postgres write with `SELECT ... FOR UPDATE` row-level locking to
> serialise concurrent state transitions. See [store_layer.md](../storage/store_layer.md)
> for details on persistence, Kafka audit events, and budget finalisation.

---

## Configuration

| Variable | Source | Default | Description |
|---|---|---|---|
| `SLACK_DEFAULT_CHANNEL` | `os.getenv` in router | `#general` | Fallback channel if the Slack payload does not include one. |
| `SLACK_SIGNING_SECRET` | `core.slack_bot` | — | Slack app signing secret for HMAC-SHA256 verification. If unset, verification is bypassed. |
| `SLACK_BOT_TOKEN` | `core.slack_bot` | — | Bot OAuth token used to post messages via `chat.postMessage`. If unset, outbound messages are silently skipped. |

---

## Security

### Signature Verification

Both endpoints call `verify_slack_signature()` from `core.slack_bot` before any
payload parsing. The verification process:

1. If `SLACK_SIGNING_SECRET` is not set, returns `True` (development mode).
2. Rejects requests where the timestamp is missing or older than 5 minutes.
3. Computes `HMAC-SHA256("v0:{timestamp}:{body}", signing_secret)`.
4. Compares the result with the `X-Slack-Signature` header using
   `hmac.compare_digest` (constant-time comparison).

### Error Responses

| HTTP Status | Condition |
|---|---|
| `401` | Invalid or missing Slack signature. |
| `400` | Malformed JSON (events) or malformed form-encoded payload (interactions). |
| `200` | All successfully processed requests return `{"ok": true}`. |

### Internal Errors

Agent dispatch failures and HITL processing errors are caught, logged, and
reported back to the Slack channel as user-facing error messages. The endpoints
themselves always return `200 {"ok": true}` to Slack to prevent Slack from
retrying the webhook.

---

## Concurrency Model

The router uses **daemon threads** for fire-and-forget background work to avoid
blocking the HTTP response to Slack (Slack expects a response within 3 seconds):

| Path | Background Work | Thread Target |
|---|---|---|
| `app_mention` | Enqueue agent job + send acknowledgement | `_reply()` closure |
| `hitl_approve_` | Resume SDLC pipeline | `_resume()` closure |

Pipeline resume functions may take minutes (coding, testing, MR creation). By
running them in a daemon thread, the router returns `{"ok": true}` immediately
while the pipeline continues asynchronously.

> **Concurrency note:** State transitions via `update_run_state()` use Postgres
> row-level locks, making them safe even if a Slack button click and a
> concurrent API call (e.g. from [sdlc_router.md](../sdlc/sdlc_router.md)) race to
> approve the same run.

---

## Related Documentation

| Module | Relationship |
|---|---|
| [sdlc_router.md](../sdlc/sdlc_router.md) | HTTP API equivalents for HITL approve/reject/resume (`approve_run`, `reject_run`, `resume_run`). |
| [shared_core_sdlc_pipeline.md](../sdlc/shared_core_sdlc_pipeline.md) | SDLC pipeline resume functions and state machine invoked after Slack HITL approvals. |
| [store_layer.md](../storage/store_layer.md) | `sdlc_store` persistence layer for run state, events, and audit trail. |
| [core_infrastructure.md](../core/core_infrastructure.md) | `core.slack_bot`, `core.job_queue`, and `core.logger` utilities. |
| [connectors_integrations.md](../connectors/connectors_integrations.md) | `SlackAdapter` for outbound Slack connector actions (separate from this inbound webhook router). |
| [chat_agent_execution_workers.md](../workers/chat_agent_execution_workers.md) | `workers.agent_worker.run_agent_job` — the worker that consumes agent jobs enqueued from `app_mention`. |
| [teams_router.md](teams_router.md) | Analogous router for Microsoft Teams integration. |
