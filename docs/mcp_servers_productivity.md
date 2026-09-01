# MCP Servers — Productivity

## Introduction

The **mcp_servers_productivity** module provides a suite of Model Context Protocol (MCP) servers that expose everyday productivity tools — calendar management, email triage, task tracking, and applicant tracking — to AI agents and workflows. Each server is a thin wrapper that registers tool functions (implemented in the [shared_integrations](shared_integrations.md) module) as JSON-RPC-callable MCP tools, inheriting all protocol handling, compliance gating, and audit logging from the [mcp_servers_base](mcp_servers_base.md) module.

A defining design principle across all four servers is the **outbox pattern**: operations that would normally have side effects (sending an email, booking a meeting, updating an ATS pipeline stage) are *not* executed directly. Instead, a draft or proposal file is written to a configurable outbox directory, deferring the actual action to a human reviewer or a separately-gated, `critical=true` dispatch tool. This keeps the productivity servers safe for autonomous agent use while preserving a full audit trail.

---

## Module Structure

```
mcp_servers_productivity
├── calendar_tools_server.py   → CalendarToolsMCPServer
├── email_tools_server.py      → EmailToolsMCPServer
├── task_tracker_server.py     → TaskTrackerMCPServer
└── ats_tools_server.py        → ATSToolsMCPServer
```

| Server | `server_name` | Tools Exposed | Use Cases |
|---|---|---|---|
| `CalendarToolsMCPServer` | `calendar_tools` | `list_calendars`, `get_busy`, `find_free_slots`, `draft_event` | Meeting notes (57), interview scheduling (63), exec inbox (86), calendar management (87) |
| `EmailToolsMCPServer` | `email_tools` | `list_messages`, `read_message`, `draft_reply` | Exec inbox triage (86), candidate follow-up drafting (64) |
| `TaskTrackerMCPServer` | `task_tracker` | `create_task`, `list_tasks`, `update_task` | Action items from meeting notes (57), onboarding checklist (65), executive inbox delegation (86) |
| `ATSToolsMCPServer` | `ats_tools` | `list_pipeline`, `score_keyword_overlap`, `propose_stage_update` | Resume-to-JD matching (62), interview scheduling (63), candidate follow-up sequences (64) |

---

## Architecture

### Class Hierarchy

All four servers inherit from `BaseMCPServer` and follow an identical registration pattern:

```mermaid
classDiagram
    class BaseMCPServer {
        +server_name: str
        +server_version: str
        -_tools: Dict~str, MCPTool~
        -_sessions: Dict~str, dict~
        +_setup_tools()*
        +_register(tool: MCPTool)
        +handle_message(body, session_id) dict
        +run_stdio()
        +sse_stream(session_id)
        +handle_streamable_http(body, session_id, user_id)
        -_audit(tool_name, inputs, output, duration_ms)
    }

    class MCPTool {
        +name: str
        +description: str
        +fn: Callable
        +input_schema: Dict
        +pci_audit: bool
    }

    class CalendarToolsMCPServer {
        +server_name = "calendar_tools"
        +_setup_tools()
    }

    class EmailToolsMCPServer {
        +server_name = "email_tools"
        +_setup_tools()
    }

    class TaskTrackerMCPServer {
        +server_name = "task_tracker"
        +_setup_tools()
    }

    class ATSToolsMCPServer {
        +server_name = "ats_tools"
        +_setup_tools()
    }

    BaseMCPServer <|-- CalendarToolsMCPServer
    BaseMCPServer <|-- EmailToolsMCPServer
    BaseMCPServer <|-- TaskTrackerMCPServer
    BaseMCPServer <|-- ATSToolsMCPServer
    BaseMCPServer o-- MCPTool : registers
```

### High-Level Architecture

```mermaid
graph TB
    subgraph "MCP Client / Agent"
        CLI["CLI / SSE / Streamable HTTP Client"]
    end

    subgraph "mcp_servers_base"
        BASE["BaseMCPServer<br/>JSON-RPC 2.0 dispatch<br/>Compliance gate<br/>Audit logging"]
    end

    subgraph "mcp_servers_productivity"
        CAL["CalendarToolsMCPServer"]
        EMAIL["EmailToolsMCPServer"]
        TASK["TaskTrackerMCPServer"]
        ATS["ATSToolsMCPServer"]
    end

    subgraph "shared_integrations (tool implementations)"
        CAL_T["calendar_tools_tools.py<br/>list_calendars, get_busy,<br/>find_free_slots, draft_event"]
        EMAIL_T["email_tools_tools.py<br/>list_messages, read_message,<br/>draft_reply"]
        TASK_T["task_tracker_tools.py<br/>create_task, list_tasks,<br/>update_task"]
        ATS_T["ats_tools_tools.py<br/>list_pipeline, score_keyword_overlap,<br/>propose_stage_update"]
    end

    subgraph "File System"
        DATA["data_dir<br/>(.ics files, mailbox,<br/>pipeline CSV, tasks JSON)"]
        OUTBOX["outbox_dir<br/>(draft .ics, .eml,<br/>proposals)"]
    end

    subgraph "Database"
        AUDIT["tool_audit_log table"]
    end

    CLI -->|"JSON-RPC 2.0"| BASE
    BASE --> CAL
    BASE --> EMAIL
    BASE --> TASK
    BASE --> ATS

    CAL --> CAL_T
    EMAIL --> EMAIL_T
    TASK --> TASK_T
    ATS --> ATS_T

    CAL_T -->|"read"| DATA
    EMAIL_T -->|"read"| DATA
    ATS_T -->|"read"| DATA
    TASK_T -->|"read/write"| DATA

    CAL_T -->|"write draft"| OUTBOX
    EMAIL_T -->|"write draft"| OUTBOX
    ATS_T -->|"write proposal"| OUTBOX

    BASE -->|"pci_audit=True"| AUDIT
```

---

## Component Documentation

### CalendarToolsMCPServer

**File:** `mcp/servers/calendar_tools_server.py`
**Server name:** `calendar_tools`

Wraps four calendar utility functions from `tools/calendar_tools_tools.py`. The server reads `.ics` files from a configured data directory and writes draft events to an outbox.

| Tool | PCI Audit | Description | Key Parameters |
|---|---|---|---|
| `list_calendars` | No | Lists all `.ics` files under the data root | — |
| `get_busy` | No | Returns busy intervals (start, end, title) from a calendar | `calendar_path` (required) |
| `find_free_slots` | No | Finds common free slots across multiple calendars on a given date within working hours | `calendar_paths` (required), `date` YYYY-MM-DD (required), `duration_min` (default 60), `earliest`/`latest` HH:MM (optional) |
| `draft_event` | **Yes** | Writes a tentative `.ics` event to the outbox (not booked) | `title`, `start_iso`, `end_iso`, `attendees` (all required) |

**Free-slot algorithm:** The tool iterates from the earliest working hour to the latest in 30-minute increments. A slot of `duration_min` length is considered free if it does not overlap any busy interval. Results are capped at 10 slots.

---

### EmailToolsMCPServer

**File:** `mcp/servers/email_tools_server.py`
**Server name:** `email_tools`

Wraps three email utility functions from `tools/email_tools_tools.py`. The server reads messages from a configured mailbox directory and writes draft replies to an outbox.

| Tool | PCI Audit | Description | Key Parameters |
|---|---|---|---|
| `list_messages` | No | Lists message metadata (id, from, subject, date) | — |
| `read_message` | No | Reads the full body of a message by id | `message_id` (required) |
| `draft_reply` | **Yes** | Writes a DRAFT reply `.eml` to the outbox (never sends) | `message_id`, `body` (both required) |

> **Design note:** Sending is intentionally absent from this server. Actual email dispatch is a gated tool registered separately with `critical=true`, ensuring a human-in-the-loop or an explicitly approved automation path is required.

---

### TaskTrackerMCPServer

**File:** `mcp/servers/task_tracker_server.py`
**Server name:** `task_tracker`

Wraps three task management functions from `tools/task_tracker_tools.py`. Tasks are persisted as a JSON file in the data directory.

| Tool | PCI Audit | Description | Key Parameters |
|---|---|---|---|
| `create_task` | **Yes** | Creates a task with a UUID-based 8-char id, status `open` | `title` (required), `owner`, `due` YYYY-MM-DD, `details` |
| `list_tasks` | No | Lists tasks, optionally filtered by status and/or owner | `status` (open/done), `owner` |
| `update_task` | **Yes** | Updates a task's status, owner, or due date | `task_id` (required), `status`, `owner`, `due` |

**Persistence:** Tasks are stored as a JSON array in the data directory. Each `create_task` and `update_task` call loads the full list, modifies it, and writes it back atomically.

---

### ATSToolsMCPServer

**File:** `mcp/servers/ats_tools_server.py`
**Server name:** `ats_tools`

Wraps three applicant tracking functions from `tools/ats_tools_tools.py`. The server reads candidate pipeline data from a CSV file and writes stage-change proposals to an outbox.

| Tool | PCI Audit | Description | Key Parameters |
|---|---|---|---|
| `list_pipeline` | No | Lists candidates from the pipeline CSV, optionally filtered by stage | `stage` |
| `score_keyword_overlap` | No | Deterministic keyword-coverage score (0–100) of a resume against JD requirements | `resume_text`, `jd_must_have[]`, `jd_nice_to_have[]` (all required) |
| `propose_stage_update` | **Yes** | Writes a PROPOSED stage change to the outbox for recruiter confirmation (no direct ATS write) | `candidate_id`, `new_stage`, `rationale` (all required) |

**Scoring formula:** The score is weighted as `70% × (must_have_hits / total_must_have) + 30% × (nice_to_have_hits / total_nice_to_have)`, rounded to the nearest integer. A requirement phrase is considered a "hit" if any word of 4+ characters from the phrase appears in the lowercased resume text.

---

## Request Processing Flow

The following sequence diagram illustrates how a `tools/call` request flows through the system, from the MCP client through the base server's compliance and audit gates to the underlying tool function:

```mermaid
sequenceDiagram
    participant Client as MCP Client
    participant Base as BaseMCPServer
    participant Server as Productivity Server<br/>(e.g. CalendarToolsMCPServer)
    participant Tool as Tool Function<br/>(e.g. draft_event)
    participant FS as File System<br/>(data_dir / outbox)
    participant DB as tool_audit_log

    Client->>Base: JSON-RPC tools/call<br/>{name: "draft_event", arguments: {...}}

    Base->>Base: Compliance check on input
    alt Input blocked
        Base-->>Client: {content: "[BLOCKED] ...", isError: true}
    end

    Base->>Server: Look up registered tool
    Server->>Tool: Call fn(**arguments)

    alt Read operation (e.g. list_calendars)
        Tool->>FS: Read .ics / mailbox / CSV / JSON
        FS-->>Tool: Data
        Tool-->>Server: Result (list of dicts)
    else Write/draft operation (e.g. draft_event)
        Tool->>FS: Write draft to outbox
        FS-->>Tool: File path
        Tool-->>Server: Result ({status, file})
    end

    Server-->>Base: Result string

    Base->>Base: Compliance check on output
    alt Output blocked
        Base-->>Client: {content: "[OUTPUT BLOCKED] ...", isError: true}
    end

    alt pci_audit == True
        Base->>DB: INSERT tool_audit_log<br/>(tool_name, inputs, output, duration_ms)
    end

    Base-->>Client: JSON-RPC response<br/>{content: [{type: "text", text: result}]}
```

---

## Outbox Pattern

A critical safety mechanism shared by all four servers is the **outbox pattern**. Write operations that could have real-world consequences never execute the action directly. Instead, they produce a draft or proposal artifact in a designated outbox directory:

```mermaid
flowchart LR
    subgraph "Agent Request"
        REQ["Agent calls tool<br/>e.g. draft_event / draft_reply /<br/>propose_stage_update / create_task"]
    end

    subgraph "Tool Execution"
        TOOL["Tool function executes"]
        OUTBOX["Outbox directory"]
    end

    subgraph "Human / Gated Review"
        REVIEW["Human reviews draft<br/>OR<br/>Gated dispatch tool<br/>(critical=true)"]
    end

    subgraph "Action"
        SEND["Email sent /<br/>Meeting booked /<br/>ATS stage updated"]
    end

    REQ --> TOOL
    TOOL -->|"writes .ics / .eml / .txt / .json"| OUTBOX
    OUTBOX --> REVIEW
    REVIEW -->|"approved"| SEND
    REVIEW -->|"rejected"| DISCARD["Draft discarded"]
```

| Server | Outbox Artifact | Format | Deferred Action |
|---|---|---|---|
| Calendar | `draft_<start>_<title>.ics` | ICS with `STATUS:TENTATIVE` | Meeting booking |
| Email | `draft_re_<subject>.eml` | EML with `X-Status: DRAFT` | Email send |
| ATS | `proposed_<candidate>_<stage>.txt` | Plain text proposal | ATS stage update |
| Task Tracker | tasks JSON (direct write) | JSON | — (tasks are low-risk, written directly) |

> **Note:** The Task Tracker is the exception — `create_task` and `update_task` write directly to the tasks JSON file because task management is considered low-risk. However, both operations still carry `pci_audit=True` for full audit logging.

---

## Dependencies

```mermaid
graph TD
    subgraph "This Module"
        PROD["mcp_servers_productivity"]
    end

    subgraph "Base Infrastructure"
        BASE["mcp_servers_base<br/>BaseMCPServer, MCPTool"]
    end

    subgraph "Tool Implementations"
        SI["shared_integrations<br/>calendar_tools, email_tools,<br/>task_tracker_tools, ats_tools"]
    end

    subgraph "MCP System"
        MCP["mcp_system<br/>MCPRegistry, MCPBridge,<br/>SSE/Stdio clients"]
    end

    subgraph "Core Infrastructure"
        CORE["core_infrastructure<br/>Compliance checks,<br/>DB session for audit"]
    end

    PROD -->|"inherits"| BASE
    PROD -->|"imports tool functions"| SI
    BASE -->|"compliance gate"| CORE
    BASE -->|"audit logging"| CORE
    MCP -->|"registers & transports"| PROD
```

### Dependency Summary

| Dependency | Type | Purpose |
|---|---|---|
| [mcp_servers_base](mcp_servers_base.md) | Inheritance | `BaseMCPServer` provides JSON-RPC 2.0 dispatch, compliance gating, audit logging, and stdio/SSE/streamable-HTTP transports |
| [shared_integrations](shared_integrations.md) | Import | Tool function implementations (`calendar_tools_tools`, `email_tools_tools`, `task_tracker_tools`, `ats_tools_tools`) |
| [mcp_system](mcp_system.md) | Registration | `MCPRegistry` discovers and registers server instances; `MCPBridge` and client transports connect agents to servers |
| [core_infrastructure](core_infrastructure.md) | Runtime | Compliance checks (`_compliance_check`) on tool inputs/outputs; database session for `tool_audit_log` inserts |

---

## Transport Support

All four servers inherit the full transport stack from `BaseMCPServer`:

| Transport | Method | Use Case |
|---|---|---|
| **stdio** | `run_stdio()` | Standalone execution via `__main__` block; reads JSON-RPC from stdin, writes to stdout |
| **SSE** | `sse_stream(session_id)` / `handle_sse_message(body, session_id)` | Persistent Server-Sent Events stream with 15s keep-alive pings; messages POSTed to `/mcp/{name}/message` |
| **Streamable HTTP** | `handle_streamable_http(body, session_id, user_id)` | MCP spec 2024-11-05; inline JSON-RPC response with `Mcp-Session-Id` header; used by CLI v0.2.101+ |

Each server's `__main__` block runs the stdio transport:

```python
if __name__ == "__main__":
    asyncio.run(CalendarToolsMCPServer().run_stdio())
```

---

## PCI Audit & Compliance

Tools marked with `pci_audit=True` trigger full input/output logging to the `tool_audit_log` database table after execution. The base class records:

| Field | Content |
|---|---|
| `tool_name` | Registered tool name (e.g., `draft_event`) |
| `inputs` | JSON-serialized arguments |
| `output` | Result string (truncated to 2000 chars) |
| `duration_ms` | Execution time |
| `created_at` | Timestamp |

Additionally, **all** tool calls (regardless of `pci_audit`) pass through dual compliance gates — one on input and one on output — via `_compliance_check()`. If either gate blocks, the response is returned with `isError: true` and the tool function is either not called (input block) or its result is suppressed (output block).

### PCI-Audited Tools by Server

| Server | Audited Tools |
|---|---|
| Calendar | `draft_event` |
| Email | `draft_reply` |
| Task Tracker | `create_task`, `update_task` |
| ATS | `propose_stage_update` |

---

## Cross-Server Use Case Flows

Several use cases span multiple productivity servers. The diagram below illustrates how an agent might orchestrate tools across servers:

```mermaid
flowchart TB
    subgraph "Use Case 63: Interview Scheduling"
        A1["ATS: list_pipeline<br/>(stage=interview)"]
        A2["ATS: score_keyword_overlap<br/>(resume vs JD)"]
        A3["Calendar: find_free_slots<br/>(interviewer calendars)"]
        A4["Calendar: draft_event<br/>(tentative interview .ics)"]
        A5["Email: draft_reply<br/>(candidate invitation)"]
        A6["Task: create_task<br/>(follow-up reminder)"]

        A1 --> A2 --> A3 --> A4 --> A5 --> A6
    end

    subgraph "Use Case 86: Executive Inbox Triage"
        B1["Email: list_messages"]
        B2["Email: read_message"]
        B3["Task: create_task<br/>(delegate action item)"]
        B4["Calendar: draft_event<br/>(schedule follow-up)"]
        B5["Email: draft_reply<br/>(acknowledge sender)"]

        B1 --> B2 --> B3
        B2 --> B4
        B2 --> B5
    end

    subgraph "Use Case 57: Meeting Notes → Action Items"
        C1["Calendar: get_busy<br/>(verify attendance)"]
        C2["Task: create_task<br/>(per action item)"]
        C3["Task: list_tasks<br/>(track open items)"]

        C1 --> C2 --> C3
    end
```

---

## Configuration

Tool implementations in [shared_integrations](shared_integrations.md) rely on environment-configured paths:

| Config | Used By | Purpose |
|---|---|---|
| `_DATA_DIR` | Calendar, Email, ATS, Task Tracker | Root directory for `.ics` files, mailbox, pipeline CSV, and tasks JSON |
| `_OUTBOX_DIR` | Calendar, Email, ATS | Directory where draft/proposal artifacts are written |
| `_WORK_HOURS` | Calendar | Default working hours (e.g., `09:00-18:00`) for `find_free_slots` |
| `_TIMEZONE` | Calendar | Timezone for draft `.ics` events |
| `_PIPELINE_CSV` | ATS | Filename of the candidate pipeline CSV within `_DATA_DIR` |

---

## Related Documentation

- [mcp_servers_base](mcp_servers_base.md) — `BaseMCPServer` and `MCPTool` base classes, JSON-RPC protocol handling, transport implementations
- [shared_integrations](shared_integrations.md) — Tool function implementations for calendar, email, task tracker, and ATS tools
- [mcp_system](mcp_system.md) — `MCPRegistry`, `MCPBridge`, and client transports for server registration and agent connectivity
- [core_infrastructure](core_infrastructure.md) — Compliance checking and database session management for audit logging
- [mcp_servers_collaboration](mcp_servers_collaboration.md) — Sibling MCP servers for Confluence, Jira, and GitLab
- [mcp_servers_content](mcp_servers_content.md) — Sibling MCP servers for document tools, doc generation, translation, and LMS
- [mcp_servers_data](mcp_servers_data.md) — Sibling MCP servers for data tools, database access, and KB search
- [mcp_servers_platform](mcp_servers_platform.md) — Sibling MCP server for platform-level agent and health operations
