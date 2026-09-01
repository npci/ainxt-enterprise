# ChatPanel Module Overview

## Purpose

The **ChatPanel** module is the workflow execution chat surface in the ABStudio workflow editor. It renders an interactive, streaming chat interface where users can send prompts, watch workflow runs progress, inspect agent thinking timelines, approve human-in-the-loop (HITL) tool calls, and download generated files. The module consumes Server-Sent Events (SSE) from the backend execution endpoints and maps them into a live transcript of user and assistant messages.

## Architecture

`ChatPanel.jsx` is organized as a single file containing four logical sub-modules. The main component orchestrates state, streaming, and rendering, while the sub-modules handle focused responsibilities.

```mermaid
flowchart TB
    subgraph ChatPanel["ChatPanel.jsx"]
        Core["ChatPanelCore<br/>(state, SSE, send/resume, thread history)"]
        Actions["ChatActions<br/>(regenerate, copy, round chips)"]
        Content["MessageContent<br/>(CodeBlock, ToolCallDetails, condition snapshots)"]
        Files["FileHandling<br/>(FileDownloadCard, authenticated downloads)"]
    end

    subgraph Stores["Frontend Stores"]
        WS["workflowStore<br/>(chatMessages, executionLogs, isExecuting)"]
    end

    subgraph Backend["ABStudio Backend"]
        Exec["api_execution<br/>(/run-stream, /resume-stream)"]
        Docs["api_documents<br/>(/generated-files/*)"]
    end

    User -->|sends message| Core
    Core -->|POST /run-stream| Exec
    Exec -->|SSE events| Core
    Core -->|reads/writes| WS
    Core -->|renders| Actions
    Core -->|renders| Content
    Core -->|renders| Files
    Files -->|auth fetch| Docs
```

### Component Breakdown

| Sub-module | Responsibility |
|---|---|
| **ChatPanelCore** | Main React component: local state, SSE stream handling, send/resume logic, thread history loading, attachment handling, HITL cards, and message mapping. |
| **ChatActions** | Message-level action handlers (`handleRegenerate`, `handleCopy`) and timeline rendering helper (`renderRoundChip`) for loop iterations. |
| **MessageContent** | Specialized renderers for code blocks, tool-call argument cards, and loop-condition evaluation snapshots. |
| **FileHandling** | Generated-file discovery, authenticated download cards, and markdown link overrides for `/generated-files/*` URLs. |

### Data Flow

```mermaid
sequenceDiagram
    participant U as User
    participant CP as ChatPanelCore
    participant WS as workflowStore
    participant BE as Backend /run-stream
    participant Sub as Sub-modules

    U->>CP: Type message & send
    CP->>WS: Append user message, set isExecuting
    CP->>BE: POST /run-stream
    BE-->>CP: SSE agent_start, agent_token, loop_iter_summary, ...
    CP->>WS: Update executionLogs, streamingContent
    CP->>Sub: Render messages with CodeBlock, ToolCallDetails, FileDownloadCard, round chips
    BE-->>CP: SSE complete
    CP->>WS: Append assistant message, clear streaming state
    U->>Sub: Click regenerate / copy / download
    Sub->>WS: Update messages or trigger authenticated download
```

## Core Components Documentation

- **[ChatPanelCore](ChatPanelCore.md)** — Main component state, SSE handling, send/resume, thread history, attachments, and HITL cards.
- **[ChatActions](ChatActions.md)** — Regenerate, copy, and loop-round chip rendering.
- **[MessageContent](MessageContent.md)** — `CodeBlock`, `ToolCallDetails`, and `renderConditionSnapshot`.
- **[FileHandling](FileHandling.md)** — `FileDownloadCard`, `fallbackDownload`, and authenticated generated-file downloads.

## Key Dependencies

- **[workflowStore](workflowStore.md)** — Provides `chatMessages`, `executionLogs`, `isExecuting`, and run context.
- **[api_execution](api_execution.md)** — Backend endpoints that stream workflow execution events.
- **[api_documents](api_documents.md)** — Serves generated files consumed by `FileHandling`.
- **[utils/threadHelpers.js](utils.md)** — Maps backend history to UI messages.
- **[utils/editorPersistence.js](utils.md)** — Persists composer drafts per workflow/thread.