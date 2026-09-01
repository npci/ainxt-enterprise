# ai_ui_frontend_hooks

## Overview

`ai_ui_frontend_hooks` is a small React hooks layer inside the `ai-ui` frontend package. It encapsulates two distinct cross-cutting concerns that are reused by multiple UI features:

1. **Desktop integration** — detection of, and communication with, the AiNxt Electron desktop host (`window.ainxtDesktop`). This includes file-system access, workspace watching, clipboard integration, local MCP registration, token sync, and the two Cowork local-agent modes (code/office).
2. **Drag-and-drop file handling** — a robust `useFileDrop` hook that attaches native HTML5 drag-and-drop listeners to any DOM element, with lazy mounting support and MIME-type filtering.

Both hooks are designed to degrade gracefully when the capability is unavailable (browser vs. desktop, disabled drop zones, etc.), keeping calling components free of environment guards.

## Architecture

```mermaid
graph TB
    subgraph ai_ui_frontend_hooks
        A[useDesktop.js]
        B[useFileDrop.js]
    end

    A -->|window.ainxtDesktop| C[Electron main process]
    A --> D[Cowork local agent]
    A --> E[Cowork Office mode]
    A --> F[Local MCP server]
    A --> G[OS notifications / clipboard / filesystem]

    B --> H[Drop-zone DOM element]
    H --> I[Native dragenter/dragleave/dragover/drop]
    I --> J[onFiles callback]
```

### Module placement

- `ai-ui/src/hooks/useDesktop.js` — desktop bridge and Cowork helpers.
- `ai-ui/src/hooks/useFileDrop.js` — reusable drag-and-drop React hook.

These hooks are consumed by higher-level components in `ai_ui_frontend` (for example `knowledge_base`, `cowork_desktop`, `chat`, and `kb_chat`). They do not contain business logic themselves; they only abstract platform capabilities and browser events.

## Sub-modules

| Sub-module | File | Responsibility |
|------------|------|----------------|
| Desktop hooks | `useDesktop.js` | Detect desktop runtime, expose Electron APIs, manage Cowork sessions, sync auth tokens, local MCP, clipboard, and filesystem helpers. |
| File drop hook | `useFileDrop.js` | Provide a ref-callback based React hook for drag-and-drop file selection with lazy mounting and accept filtering. |

Detailed documentation:

- [ai_ui_frontend_hooks_desktop](ai_ui_frontend_hooks_desktop.md)
- [ai_ui_frontend_hooks_file_drop](ai_ui_frontend_hooks_file_drop.md)

## Data flow

### Desktop capability detection

```mermaid
sequenceDiagram
    participant Component as React component
    participant Hook as useDesktop helpers
    participant Electron as window.ainxtDesktop
    participant Main as Electron main process

    Component->>Hook: import { isDesktop, pickFile, coworkRun, ... }
    Hook->>Electron: typeof window.ainxtDesktop?.isDesktop
    Electron-->>Hook: desktop object or null
    alt desktop available
        Component->>Hook: pickFile()
        Hook->>Electron: desktop.pickFile()
        Electron->>Main: showOpenDialog
        Main-->>Electron: selected paths
        Electron-->>Hook: file list
    else browser fallback
        Hook-->>Component: safe default (empty array / null)
    end
```

### File drop handling

```mermaid
sequenceDiagram
    participant User
    participant DOM as Drop-zone element
    participant Hook as useFileDrop
    participant Component as Parent component

    User->>DOM: drag files over
    DOM->>Hook: dragenter / dragover
    Hook->>Hook: dragCounter++, setIsDragging(true)
    User->>DOM: drop files
    DOM->>Hook: drop event
    Hook->>Hook: filter by accept MIME patterns
    Hook->>Component: onFiles(validFiles, invalidFiles)
    Component->>Component: upload / validate
```

## Integration notes

- `useDesktop.js` is the primary integration point between the web frontend and the `desktop_app` module. For the Electron side of the contract, see [desktop_app](desktop_app.md).
- `useFileDrop.js` is used by upload surfaces such as `knowledge_base` and chat attachment areas. It intentionally avoids React synthetic events and uses native DOM listeners with `{ passive: false }` so that `preventDefault()` can suppress the browser's default drag-and-drop security popup.
- Both hooks are stateless utilities; they do not depend on the global store or router and can be imported by any component.
