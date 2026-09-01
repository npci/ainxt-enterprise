# Checkpoint Workflow Store

## Introduction

The **Checkpoint Workflow Store** module provides the persistence layer for workflow chat history, Human-in-the-Loop (HITL) interrupt snapshots, per-node output caching, audit trails, loop cross-run memory, and durable replay state. It defines an abstract storage contract (`CheckpointStore`) with two concrete implementations — a file-backed JSON store for local development and a PostgreSQL-backed store for production. The module is consumed exclusively by the `NativeEngine` (see [engine_native_engine](engine_native_engine.md)), which selects the appropriate backend at startup based on whether `POSTGRES_HOST` is configured.

---

## Architecture Overview

```mermaid
graph TB
    subgraph "Engine Layer"
        NE["NativeEngine<br/>(engine_native_engine)"]
    end

    subgraph "Checkpoint Workflow Store"
        CS["CheckpointStore<br/>(abstract interface)"]
        FCS["FileCheckpointStore<br/>(JSON file backend)"]
        PCS["PostgresCheckpointStore<br/>(PostgreSQL backend)"]
    end

    subgraph "Shared Data Types"
        CM["ChatMessage"]
        TS["ThreadSummary"]
        ST["summarise_thread()"]
    end

    subgraph "External Dependencies"
        CFG["app.core.config<br/>postgres_enabled()"]
        POOL["app.core.db_pool<br/>SHARED_POOL"]
        LOG["core.logger"]
    end

    NE -->|"selects at startup"| CS
    CS -->|"implements"| FCS
    CS -->|"implements"| PCS
    FCS --> CM
    FCS --> TS
    FCS --> ST
    PCS --> CM
    PCS --> TS
    PCS --> ST
    PCS -->|"borrows connection pool"| POOL
    PCS -->|"checks env flag"| CFG
    FCS --> LOG
    PCS --> LOG
```

### Backend Selection Logic

The `NativeEngine.startup()` method determines which backend to use:

```mermaid
flowchart TD
    A["NativeEngine.startup()"] --> B{"postgres_enabled()?"}
    B -->|"Yes"| C["Create PostgresCheckpointStore"]
    C --> D{"startup() succeeds?"}
    D -->|"Yes"| E["Use PostgreSQL backend"]
    D -->|"No (exception)"| F["Fallback to FileCheckpointStore"]
    B -->|"No"| F
    F --> G["Use File backend"]
```

---

## Core Components

### 1. `CheckpointStore` (Abstract Base Class)

**File:** `ABStudio/backend/app/checkpoint/store.py`

Defines the complete storage contract that every backend must fulfil. The interface is organized into seven functional groups, each with default no-op implementations so that limited/legacy backends remain functional:

| Group | Methods | Purpose |
|-------|---------|---------|
| **Lifecycle** | `startup()`, `shutdown()` | Initialise / release resources |
| **Chat History** | `save_messages()`, `load_messages()`, `list_threads()`, `delete_thread()`, `delete_threads_for_workflow()` | Persist and retrieve thread message lists |
| **HITL Interrupts** | `save_pending_interrupt()`, `load_pending_interrupt()`, `delete_pending_interrupt()` | Snapshot paused workflow runs for resume |
| **Per-Node Outputs** | `save_node_output()`, `load_node_output()` | Cache latest output per (thread, node) for the Loop picker |
| **Audit Trails** | `save_loop_iteration()`, `save_condition_routing()`, `save_hitl_decision()` | Best-effort persistent records of loop iterations, condition routings, and HITL decisions |
| **Loop Memory** | `save_loop_lesson()`, `load_loop_lessons()` | Cross-run reflection digests keyed by (workflow_id, node_id) |
| **Durable Replay** | `save_run_step()`, `load_run_state()`, `append_run_event()`, `replay_events()` | Authoritative per-step state + append-only event log for crash recovery and deterministic replay |

> **Design principle:** Every method beyond the core lifecycle/chat-history group has a default no-op implementation. This ensures that `FileCheckpointStore` (which only implements chat history, HITL, node outputs, and loop lessons) remains a valid drop-in without needing to stub every audit method.

### 2. `FileCheckpointStore`

**File:** `ABStudio/backend/app/checkpoint/store.py`

A zero-dependency JSON file store ideal for local development and single-instance deployments.

- **Storage:** Single JSON file at `<backend_root>/data/chat_history.json` (configurable via constructor `path=`)
- **Schema:** `{ thread_id: { workflow_id, messages: [{role, content, generated_files?}], last_updated, pending_interrupt?, node_outputs? } }`
- **Concurrency:** Thread-safe via `threading.Lock` with atomic writes (write to `.tmp` then `os.replace`)
- **Loop lessons:** Stored under a synthetic top-level key `__loop_lessons__` to avoid collision with UUID-based thread IDs; capped at 5 most recent digests per (workflow_id, node_id)

**Implemented methods:** All chat history, HITL, per-node output, and loop lesson methods. Audit trail and durable replay methods inherit the default no-ops.

### 3. `PostgresCheckpointStore`

**File:** `ABStudio/backend/app/checkpoint/postgres_store.py`

Production-grade PostgreSQL store that reuses the platform's shared connection pool (see [core_db_pool](core_db_pool.md)).

- **Connection:** Borrows from `app.core.db_pool.SHARED_POOL` — never opens its own pool
- **Schema:** Auto-creates 9 tables on `startup()` with appropriate indexes
- **All methods implemented:** Full support for every interface method including audit trails and durable replay

#### Database Schema

```mermaid
erDiagram
    chat_threads ||--o| pending_interrupts : "1:1 optional"
    chat_threads ||--o{ chat_thread_node_outputs : "1:N"
    chat_threads ||--o{ loop_iterations : "1:N"
    chat_threads ||--o{ condition_routings : "1:N"
    chat_threads ||--o{ hitl_decisions : "1:N"
    chat_threads ||--o{ run_steps : "1:N"
    chat_threads ||--o{ run_events : "1:N"
    loop_lessons }o--|| workflows : "N:1 via workflow_id"

    chat_threads {
        TEXT thread_id PK
        TEXT workflow_id
        JSONB messages
        TIMESTAMPTZ last_updated
    }
    pending_interrupts {
        TEXT thread_id PK
        JSONB snapshot
        TIMESTAMPTZ created_at
    }
    chat_thread_node_outputs {
        TEXT thread_id PK
        TEXT node_id PK
        TEXT workflow_id
        TEXT agent
        TEXT output
        TIMESTAMPTZ updated_at
    }
    loop_iterations {
        BIGSERIAL id PK
        TEXT thread_id
        TEXT workflow_id
        TEXT node_id
        INT iteration
        TEXT mode
        INT total
        DOUBLE_PRECISION score
        TEXT changes
        BOOLEAN will_continue
        JSONB case_results
        TEXT output_preview
        TIMESTAMPTZ created_at
    }
    loop_lessons {
        BIGSERIAL id PK
        TEXT workflow_id
        TEXT node_id
        TEXT digest
        TIMESTAMPTZ created_at
    }
    condition_routings {
        BIGSERIAL id PK
        TEXT thread_id
        TEXT workflow_id
        TEXT node_id
        TEXT matched_case_id
        TEXT matched_label
        TEXT matched_expression
        TEXT upstream_output_preview
        JSONB evaluated_state
        TEXT target_node_id
        TIMESTAMPTZ created_at
    }
    hitl_decisions {
        BIGSERIAL id PK
        TEXT thread_id
        TEXT workflow_id
        TEXT node_id
        TEXT reason
        TEXT hitl_mode
        TEXT decision
        TEXT human_input
        TEXT user_id
        TIMESTAMPTZ created_at
    }
    run_steps {
        TEXT thread_id PK
        INT step_index PK
        TEXT workflow_id
        TEXT node_id
        TEXT node_type
        TEXT status
        INT attempt
        JSONB input_snapshot
        TEXT output_ref
        TEXT idempotency_key
        TIMESTAMPTZ updated_at
    }
    run_events {
        BIGSERIAL id PK
        TEXT thread_id
        TEXT workflow_id
        INT step_index
        TEXT event_type
        JSONB payload
        TIMESTAMPTZ created_at
    }
```

### 4. Shared Data Types

#### `ChatMessage`
A dataclass representing a single chat message:
- `role`: `"user"` or `"assistant"`
- `content`: The message text
- `generated_files`: Optional list of file attachment dicts (`{filename, download_url, format, path}`) — persisted so download chips survive page reload
- `usage`: Optional usage metadata dict (model, tokens_in, tokens_out, cost_usd, latency_ms)

#### `ThreadSummary`
A dataclass representing a sidebar-ready thread summary:
- `thread_id`, `title`, `last_message_preview`, `last_updated`, `message_count`
- `has_pending_interrupt`: Whether the thread has a paused HITL run
- `pending_reason`: The snapshot's `reason` field (`"ask_human"`, `"before_tool"`, `"after_response"`, `"subflow_pending"`, `"node_failed"`, `"user_cancelled"`, or `""`)

#### `summarise_thread()`
Shared helper that derives a `ThreadSummary` from a raw message list. Used by both the workflow chat store and the agent chat store (see [checkpoint_agent_chat_store](checkpoint_agent_chat_store.md)) to keep sidebar heuristics consistent.

---

## Data Flow

### Workflow Execution & Persistence Flow

```mermaid
sequenceDiagram
    participant Client
    participant NE as NativeEngine
    participant Store as CheckpointStore

    Note over NE: execute() called
    NE->>Store: load_messages(thread_id)
    Store-->>NE: chat history

    NE->>Store: save_messages(thread_id, wf_id, [user_msg])
    Note over NE: Eager user-prompt save<br/>survives HITL pause / crash

    loop Graph Traversal
        NE->>Store: save_run_step(thread_id, step_idx, node_id, "running", input_snapshot)
        NE->>Store: append_run_event(thread_id, "node_running", ...)
        NE->>Store: save_node_output(thread_id, wf_id, node_id, agent, output)

        alt HITL pause triggered
            NE->>Store: save_pending_interrupt(thread_id, snapshot)
            NE-->>Client: SSE hitl_interrupt event
            Note over NE: Run suspended — no complete event
        end

        alt Loop iteration
            NE->>Store: save_loop_iteration(thread_id, wf_id, node_id, ...)
            alt memory.write enabled
                NE->>Store: save_loop_lesson(wf_id, node_id, digest)
            end
        end

        alt Condition node
            NE->>Store: save_condition_routing(thread_id, wf_id, node_id, ...)
        end
    end

    alt Clean completion
        NE->>Store: save_messages(thread_id, wf_id, [user_msg, assistant_msg])
        NE-->>Client: SSE complete event
    end

    alt Crash / exception
        NE->>Store: save_pending_interrupt(thread_id, failure_snapshot)
        Note over NE: reason="node_failed"<br/>Client can retry via /resume-stream
    end
```

### HITL Resume Flow

```mermaid
sequenceDiagram
    participant Client
    participant NE as NativeEngine
    participant Store as CheckpointStore

    Client->>NE: resume(chain, human_input, context)
    NE->>Store: load_pending_interrupt(thread_id)
    Store-->>NE: snapshot (reason, node_id, state, extra)

    NE->>Store: delete_pending_interrupt(thread_id)
    Note over NE: Clear up-front so a crash<br/>mid-resume doesn't loop

    NE->>Store: save_hitl_decision(thread_id, wf_id, node_id, reason, decision, ...)
    Note over NE: Durable audit record

    alt reason = "ask_human"
        Note over NE: Inject human answer as tool result<br/>Re-enter agent loop
    else reason = "before_tool"
        Note over NE: approve → run queued tools<br/>reject → synthetic tool results
    else reason = "after_response"
        Note over NE: approve → continue downstream<br/>edit → re-run agent with feedback<br/>reject → end run
    else reason = "node_failed" / "user_cancelled"
        Note over NE: Re-run node from snapshot input<br/>Continue downstream
    else reason = "subflow_pending"
        Note over NE: Recursively resume inner workflow<br/>Continue parent traversal
    end

    NE-->>Client: SSE events (hitl_resumed, agent_*, complete)
```

### Durable Replay & Crash Recovery

```mermaid
flowchart TD
    subgraph "During Run (REQ-D1/D2)"
        A["Node execution begins"] --> B["save_run_step(status='running', input_snapshot)"]
        B --> C["append_run_event('node_running')"]
        C --> D["Node executes"]
        D --> E{"Success?"}
        E -->|"Yes"| F["save_run_step(status='completed', output_ref)"]
        E -->|"Crash"| G["Snapshot lost — step stays 'running'"]
        F --> H["append_run_event('node_completed')"]
    end

    subgraph "On Resume / Crash Recovery (REQ-D3/D5)"
        I["load_run_state(thread_id)"] --> J["Find last completed step"]
        J --> K["Read input_snapshot from last step"]
        K --> L["Re-drive from next step"]
    end

    subgraph "Deterministic Replay (REQ-D2/D3)"
        M["replay_events(thread_id)"] --> N["Return events ordered by (step_index, id)"]
        N --> O["Reproduce routing decisions exactly"]
    end
```

---

## Cascade Delete Behaviour

When a workflow is deleted, `delete_threads_for_workflow()` removes all dependent data:

```mermaid
flowchart LR
    WF["Workflow deleted"] --> D["delete_threads_for_workflow(workflow_id)"]

    subgraph "PostgresCheckpointStore"
        D --> P1["DELETE FROM pending_interrupts<br/>(via subquery on chat_threads)"]
        D --> P2["DELETE FROM chat_thread_node_outputs"]
        D --> P3["DELETE FROM loop_iterations"]
        D --> P4["DELETE FROM condition_routings"]
        D --> P5["DELETE FROM hitl_decisions"]
        D --> P6["DELETE FROM run_steps"]
        D --> P7["DELETE FROM run_events"]
        D --> P8["DELETE FROM chat_threads"]
    end

    subgraph "FileCheckpointStore"
        D --> F1["Filter thread records by workflow_id"]
        F1 --> F2["Pop matching top-level keys"]
        F2 --> F3["Flush to disk"]
    end
```

> **Postgres ordering note:** `pending_interrupts` has no `workflow_id` column, so its rows are removed via a subquery on `chat_threads` — this must happen **before** `chat_threads` is emptied, or the subquery matches nothing.

---

## Loop Cross-Run Memory

Loop nodes with `memory.write` enabled persist a compact reflection digest after each run. A later run of the **same loop** with `memory.read` enabled fetches those lessons and injects them into body agents' prompts via `{{loop.prior_lessons}}`.

```mermaid
flowchart TD
    subgraph "Run 1 — memory.write"
        A1["Loop completes"] --> A2["Engine formats reflection digest"]
        A2 --> A3["save_loop_lesson(wf_id, node_id, digest)"]
    end

    subgraph "Run 2 — memory.read"
        B1["Loop starts"] --> B2["load_loop_lessons(wf_id, node_id)"]
        B2 --> B3["Returns up to 5 recent digests<br/>joined with \\n---\\n, capped at 4000 chars"]
        B3 --> B4["Injected into body agent prompt<br/>via {{loop.prior_lessons}}"]
    end
```

**File store:** Stored under `__loop_lessons__` synthetic key, capped at 5 entries per (workflow_id, node_id).

**Postgres store:** Append-only `loop_lessons` table, queried with `ORDER BY created_at DESC LIMIT 5`, returned oldest-first for chronological reading.

---

## Relationship to Sibling Module

The checkpoint package contains two sibling sub-modules:

| Module | Scope | Keyed by | See |
|--------|-------|----------|-----|
| **checkpoint_workflow_store** (this module) | Workflow chat threads | `(thread_id, workflow_id)` | — |
| **checkpoint_agent_chat_store** | Standalone agent chat threads | `(thread_id, agent_id, owner_user_id)` | [checkpoint_agent_chat_store](checkpoint_agent_chat_store.md) |

Both modules share the `ChatMessage`, `ThreadSummary`, and `summarise_thread()` types defined in `store.py`. The agent chat store defines its own `AgentChatStore` abstract base (with `owner_user_id` scoping) but reuses the same data structures and the same `summarise_thread()` helper for sidebar consistency.

---

## Integration with NativeEngine

The `NativeEngine` (see [engine_native_engine](engine_native_engine.md)) is the sole consumer of this module. Key integration points:

| Engine Method | Store Method(s) Called | Context |
|---------------|----------------------|---------|
| `startup()` | `startup()` | Backend selection + table creation |
| `shutdown()` | `shutdown()` | Resource cleanup |
| `execute()` | `load_messages()`, `save_messages()`, `save_run_step()`, `append_run_event()`, `save_node_output()`, `save_pending_interrupt()`, `save_loop_iteration()`, `save_condition_routing()`, `save_loop_lesson()` | Full run lifecycle |
| `resume()` | `load_pending_interrupt()`, `delete_pending_interrupt()`, `save_hitl_decision()`, `save_messages()` | HITL resume |
| `get_history()` | `load_messages()` | Chat panel reload |
| `list_threads()` | `list_threads()` | Sidebar thread list |
| `delete_thread()` | `delete_thread()` | Thread deletion |
| `delete_threads_for_workflow()` | `delete_threads_for_workflow()` | Workflow deletion cascade |
| `get_pending_interrupt()` | `load_pending_interrupt()` | HITL card re-render |
| `get_node_last_output()` | `load_node_output()` | Loop node picker |
| `clear_pending_interrupt()` | `load_pending_interrupt()`, `delete_pending_interrupt()` | Abort endpoint |

All store writes from the engine are **best-effort and fire-and-forget** via `_schedule_persist()` — a flaky store never breaks the live SSE stream. The engine maintains a bounded set of strong references to in-flight persist tasks to prevent garbage collection mid-write.

---

## Configuration

| Setting | Source | Effect |
|---------|--------|--------|
| `POSTGRES_HOST` | Environment variable | When set, `postgres_enabled()` returns `True` → `PostgresCheckpointStore` is selected |
| `chat_history.json` path | `FileCheckpointStore(path=)` constructor | Default: `<backend_root>/data/chat_history.json` |

The `agentchain_postgres_uri()` function in [core_config](core_config.md) is a deprecated compatibility shim that returns a non-empty sentinel iff `postgres_enabled()` — retained so any lingering `if agentchain_postgres_uri()` checks keep working.

---

## Summary

The Checkpoint Workflow Store module is the persistence backbone for the ABStudio workflow engine. Its abstract interface design allows seamless switching between a zero-dependency file store (development) and a full-featured PostgreSQL store (production) without any engine-level code changes. The module's seven functional groups — chat history, HITL interrupts, per-node outputs, audit trails, loop memory, and durable replay — collectively ensure that workflow runs are recoverable after crashes, auditable after completion, and resumable after human intervention.
