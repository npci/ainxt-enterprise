# Local Files Module

## Introduction

The **Local Files** module is a frontend React component (`ai-ui/src/components/LocalFiles.jsx`) that provides users of the AiNxt desktop application with a local workspace management interface. It enables users to add local folders as searchable workspaces, index their file contents for AI-powered retrieval, watch folders for live file changes, register a local MCP (Model Context Protocol) server with the backend, and receive intelligent clipboard-capture suggestions — all from a single panel within the AiNxt web UI.

The module is exclusively available when running inside the AiNxt Electron desktop app. In a standard browser context, it renders a graceful fallback prompting the user to download the desktop application.

---

## Architecture Overview

```mermaid
graph TB
    subgraph "AiNxt Desktop App (Electron)"
        MainProc["Desktop Main Process<br/>window.ainxtDesktop"]
        McpServer["Local MCP HTTP Server<br/>127.0.0.1:{port}"]
        Watcher["File Watcher<br/>(chokidar)"]
        Clipboard["Clipboard Monitor"]
    end

    subgraph "Renderer (React SPA)"
        LocalFiles["LocalFiles.jsx"]
        WorkspaceRow["WorkspaceRow"]
        ClipboardPopup["ClipboardPopup"]
        UseDesktop["useDesktop.js<br/>(bridge to Electron)"]
        Config["config.js<br/>authFetch"]
    end

    subgraph "Backend (FastAPI)"
        DesktopRouter["desktop_router.py"]
        PgVector["PostgreSQL + pgvector<br/>document_embeddings"]
        EmbedSvc["Embedding Service"]
        IndexQueue["RQ Index Queue"]
    end

    LocalFiles --> UseDesktop
    LocalFiles --> Config
    WorkspaceRow --> UseDesktop
    WorkspaceRow --> Config
    UseDesktop --> MainProc
    MainProc --> Watcher
    MainProc --> Clipboard
    MainProc --> McpServer

    LocalFiles -->|"POST /desktop/index/file"| DesktopRouter
    WorkspaceRow -->|"POST /desktop/index/batch"| DesktopRouter
    WorkspaceRow -->|"GET /desktop/index/{ws}/status"| DesktopRouter
    WorkspaceRow -->|"DELETE /desktop/index/{ws}"| DesktopRouter
    LocalFiles -->|"POST /desktop/register-mcp"| DesktopRouter
    LocalFiles -->|"GET 127.0.0.1:{port}/tools"| McpServer

    DesktopRouter --> PgVector
    DesktopRouter --> EmbedSvc
    DesktopRouter --> IndexQueue
```

### Key Architectural Decisions

| Decision | Rationale |
|---|---|
| **Desktop-only gating** | File system access, folder watching, and clipboard monitoring require Electron's native APIs. The `isDesktop` flag from `useDesktop.js` gates all functionality. |
| **Renderer-side MCP registration** | The MCP server runs in the desktop main process, but registration with the backend is done from the renderer using cookie-based `authFetch` so the user's existing web session is reused. |
| **Debounced incremental re-indexing** | File-change events are debounced per-file (2s window) to avoid redundant API calls during rapid saves. |
| **Batch vs. inline indexing** | Full folder re-indexing uses a background RQ job (`/desktop/index/batch`); single-file incremental updates are synchronous (`/desktop/index/file`). |

---

## Component Reference

### `LocalFiles` (default export)

The main panel component. Orchestrates all five operational phases of the local files feature.

**Props:**

| Prop | Type | Description |
|---|---|---|
| `onAskWithContext` | `(prompt: string) => void` | Callback invoked when the user clicks "Ask AiNxt" from the clipboard popup or a workspace row. Typically routes the prompt into the Chat component. |

**State:**

| State | Description |
|---|---|
| `workspaces` | Array of folder paths currently added as workspaces. |
| `clipPopup` | Clipboard text captured for the intelligent popup; `null` when dismissed. |
| `mcpStatus` | MCP registration lifecycle: `null` → `"registering"` → `"ok"` / `"error"`. |
| `mcpError` | Error message if MCP registration fails. |
| `recentChange` | Filename of the most recently changed file (shown in a transient banner). |
| `pendingReindex` | `useRef(Set)` tracking filenames within the debounce window to prevent duplicate re-index calls. |

**Lifecycle Effects (5 phases):**

1. **Phase 1 — Load persisted workspaces:** On mount, calls `getWatchedFolders()` to restore previously added folders from the desktop app's persistent store.
2. **Phase 2 — Workspace file change listener:** Subscribes to `onWorkspaceChange`. When a file changes, reads its content via `readFile()` and POSTs it to `/desktop/index/file` for incremental re-indexing. Includes per-file 2-second debounce.
3. **Phase 3 — Clipboard change listener:** Subscribes to `onClipboardChange`. Sets `clipPopup` state to trigger the `ClipboardPopup` component.
4. **Phase 4 — Shortcut context listener:** Subscribes to `onShortcutContext` (triggered by `Cmd+Shift+A`). If clipboard content is substantial (>10 chars), surfaces it as a suggestion popup.
5. **Phase 5 — MCP server registration:** Subscribes to `onMcpServerReady` and also attempts immediate registration. Fetches the tool list from the local MCP server, then registers with the backend via `POST /desktop/register-mcp`.

---

### `WorkspaceRow`

Renders a single workspace folder with its indexing status and action buttons.

**Props:**

| Prop | Type | Description |
|---|---|---|
| `folder` | `string` | Absolute path to the workspace folder. |
| `onRemove` | `(folder: string) => void` | Callback to remove the workspace from the parent's list. |
| `onAsk` | `(prompt: string) => void` | Callback for the "Ask about this workspace" action. |

**Internal State:**

| State | Description |
|---|---|
| `status` | `{ chunk_count, last_indexed }` fetched from `GET /desktop/index/{workspace}/status`. |
| `indexing` | Boolean indicating an active re-index operation. |
| `watching` | Boolean indicating whether the folder watcher is active. |
| `error` | Error message from the last re-index attempt. |

**Actions:**

| Action | Function | Description |
|---|---|---|
| Re-index | `reindex()` | Lists all supported files (max 500) via `listFolder()`, reads each file's content via `readFile()`, and POSTs the batch to `/desktop/index/batch`. |
| Toggle watch | `toggleWatch()` | Calls `watchFolder()` or `unwatchFolder()` on the desktop bridge. |
| Remove | `remove()` | Unwatches the folder, sends `DELETE /desktop/index/{workspace}` to remove all vectors, and calls `onRemove`. |
| Ask | `onAsk()` | Triggers the parent's `handleAsk` with a workspace-scoped prompt. |

---

### `ClipboardPopup`

A floating popup that appears when clipboard content is captured, offering context-aware quick actions.

**Props:**

| Prop | Type | Description |
|---|---|---|
| `text` | `string \| null` | The captured clipboard text. When `null`, the popup is not rendered. |
| `onAsk` | `(prompt: string) => void` | Callback for all action buttons. |
| `onDismiss` | `() => void` | Callback to close the popup. |

**Intelligent Action Buttons:**

The popup inspects the clipboard text and conditionally renders action buttons:

| Condition | Button | Prompt Sent |
|---|---|---|
| Matches `/error:\|exception\|failed\|traceback/i` | "Fix error" (red) | `Fix this error:\n{text}` |
| Starts with `{` or contains `def ` / `function ` | "Explain" (indigo) | `Explain this code:\n{text}` |
| Always | "Ask AiNxt" (indigo) | `{text}` (raw) |

---

## Data Flow

### Workspace Lifecycle

```mermaid
sequenceDiagram
    participant U as User
    participant LF as LocalFiles
    participant UD as useDesktop.js
    participant DM as Desktop Main
    participant BE as Backend (desktop_router)
    participant PG as PostgreSQL (pgvector)

    U->>LF: Click "Add folder"
    LF->>UD: pickFolder()
    UD->>DM: desktop.pickFolder()
    DM-->>UD: folder path
    LF->>UD: watchFolder(folder)
    UD->>DM: desktop.watchFolder(folder)
    LF->>LF: setWorkspaces([...prev, folder])

    Note over LF: WorkspaceRow mounts
    LF->>BE: GET /desktop/index/{ws}/status
    BE->>PG: SELECT COUNT(*), MAX(created_at)
    PG-->>BE: row
    BE-->>LF: { chunk_count, last_indexed }

    U->>LF: Click re-index
    LF->>UD: listFolder(folder, {maxFiles:500})
    UD-->>LF: file list
    loop For each file
        LF->>UD: readFile(file.path)
        UD-->>LF: { content }
    end
    LF->>BE: POST /desktop/index/batch { workspace, files }
    BE->>BE: Enqueue RQ job
    BE-->>LF: { job_id }
    LF->>BE: GET /desktop/index/{ws}/status (poll)
    BE-->>LF: updated status
```

### Live File Change Re-indexing

```mermaid
sequenceDiagram
    participant FS as Filesystem
    participant DM as Desktop Main
    participant LF as LocalFiles
    participant BE as Backend
    participant PG as pgvector

    FS->>DM: File saved
    DM->>LF: onWorkspaceChange({ filename, folder })
    LF->>LF: Check debounce set
    alt Not in debounce window
        LF->>LF: Add to pendingReindex
        LF->>DM: readFile(filename)
        DM-->>LF: { content }
        LF->>BE: POST /desktop/index/file { workspace, filename, content }
        BE->>BE: _chunk_content()
        BE->>BE: _embed_texts()
        BE->>PG: _upsert_chunks()
        BE-->>LF: { indexed, latency_ms }
    else In debounce window
        LF->>LF: Skip (already pending)
    end
    LF->>LF: Remove from pendingReindex (after 2s)
```

### MCP Server Registration

```mermaid
sequenceDiagram
    participant LF as LocalFiles
    participant DM as Desktop Main
    participant Mcp as Local MCP Server
    participant BE as Backend
    participant KV as KV Store (Redis)

    LF->>LF: onMcpServerReady handler fires
    LF->>LF: setMcpStatus("registering")
    LF->>DM: getMcpPort()
    DM-->>LF: port number
    LF->>Mcp: GET http://127.0.0.1:{port}/tools
    Mcp-->>LF: { tools: [...] }
    LF->>BE: POST /desktop/register-mcp { port, tools }
    BE->>Mcp: GET http://127.0.0.1:{port}/tools (verify reachable)
    Mcp-->>BE: { tools: [...] }
    BE->>KV: SET desktop:mcp:{user_id} (TTL 8h)
    BE-->>LF: { registered: true, port, tools }
    LF->>LF: setMcpStatus("ok")
```

---

## Dependencies

### Internal Dependencies

```mermaid
graph LR
    LocalFiles["LocalFiles.jsx"] --> UseDesktop["useDesktop.js<br/>(Electron bridge)"]
    LocalFiles --> Config["config.js<br/>(authFetch)"]
    WorkspaceRow --> UseDesktop
    WorkspaceRow --> Config
    ClipboardPopup -.->|"rendered by"| LocalFiles
    WorkspaceRow -.->|"rendered by"| LocalFiles
```

| Dependency | Module | Purpose |
|---|---|---|
| `useDesktop.js` | [ai_ui_frontend_hooks](../ui/ai_ui_frontend_hooks.md) | Electron bridge: `isDesktop`, `pickFolder`, `listFolder`, `readFile`, `watchFolder`, `unwatchFolder`, `getWatchedFolders`, `onWorkspaceChange`/`offWorkspaceChange`, `onClipboardChange`/`offClipboardChange`, `onShortcutContext`/`offShortcutContext`, `getMcpPort`, `onMcpServerReady` |
| `config.js` | [config](../infrastructure/config.md) | `authFetch` — authenticated HTTP wrapper that attaches JWT cookie credentials and correlation IDs |

### Backend Dependencies

| Endpoint | Method | Backend Module | Description |
|---|---|---|---|
| `/desktop/index/file` | POST | [desktop_router](../api/desktop_router.md) | Inline single-file indexing (incremental) |
| `/desktop/index/batch` | POST | [desktop_router](../api/desktop_router.md) | Batch indexing via RQ background job |
| `/desktop/index/{workspace}` | DELETE | [desktop_router](../api/desktop_router.md) | Remove all vectors for a workspace |
| `/desktop/index/{workspace}/status` | GET | [desktop_router](../api/desktop_router.md) | Chunk count and last-indexed timestamp |
| `/desktop/register-mcp` | POST | [desktop_router](../api/desktop_router.md) | Register local MCP server with backend |

### External Dependencies

| Package | Usage |
|---|---|
| `react` | `useState`, `useEffect`, `useRef`, `useCallback` hooks |
| `lucide-react` | Icons: `Folder`, `FolderOpen`, `Eye`, `EyeOff`, `Trash2`, `RefreshCw`, `FileText`, `Clipboard`, `Zap`, `AlertCircle`, `CheckCircle` |

---

## Component Interaction Diagram

```mermaid
graph TB
    subgraph "LocalFiles Panel"
        Header["Header<br/>'Add folder' button"]
        McpBar["MCP Status Bar"]
        ChangeBar["Live Change Indicator"]
        WSList["Workspace List"]
        ClipPopup["ClipboardPopup"]
    end

    subgraph "Per WorkspaceRow"
        Info["Folder info + status"]
        WatchBtn["Watch toggle"]
        ReindexBtn["Re-index button"]
        AskBtn["Ask button"]
        RemoveBtn["Remove button"]
    end

    Header -->|"addWorkspace()"| WSList
    WSList --> WorkspaceRow
    WorkspaceRow --> Info
    WorkspaceRow --> WatchBtn
    WorkspaceRow --> ReindexBtn
    WorkspaceRow --> AskBtn
    WorkspaceRow --> RemoveBtn

    AskBtn -->|"onAsk()"| ClipPopup
    ClipPopup -->|"onAskWithContext()"| Chat["Chat Component<br/>(parent)"]

    WatchBtn -->|"watchFolder/unwatchFolder"| Desktop["Desktop Main"]
    ReindexBtn -->|"POST /desktop/index/batch"| Backend["Backend"]
    RemoveBtn -->|"DELETE + unwatchFolder"| Desktop
    RemoveBtn -->|"DELETE /desktop/index/{ws}"| Backend
```

---

## Process Flows

### Adding a Workspace

```mermaid
flowchart TD
    A[User clicks 'Add folder'] --> B{pickFolder()}
    B -->|null| Z[No folder selected — abort]
    B -->|path| C{Already in workspaces?}
    C -->|yes| Z
    C -->|no| D[watchFolder folder]
    D --> E[setWorkspaces append]
    E --> F[WorkspaceRow mounts]
    F --> G[GET /desktop/index/{ws}/status]
    G --> H{Has chunks?}
    H -->|yes| I[Display chunk count + last indexed]
    H -->|no| J[Display 'not indexed yet']
```

### Clipboard Intelligence Flow

```mermaid
flowchart TD
    A[Clipboard changes] --> B{isDesktop?}
    B -->|no| Z[No-op]
    B -->|yes| C[onClipboardChange handler]
    C --> D[setClipPopup text]
    D --> E{ClipboardPopup renders}
    E --> F{Text matches error pattern?}
    F -->|yes| G[Show 'Fix error' button]
    F -->|no| H{Text looks like code?}
    H -->|yes| I[Show 'Explain' button]
    H -->|no| J[Skip explain button]
    G --> K[Show 'Ask AiNxt' button]
    I --> K
    J --> K
    K --> L{User clicks action}
    L --> M[handleAsk prompt]
    M --> N[onAskWithContext prompt]
    N --> O[setClipPopup null]
```

---

## API Contract

### Frontend → Backend Requests

All requests use `authFetch` from `config.js`, which attaches:
- `credentials: 'include'` (httpOnly JWT cookie)
- `x-client-request-id` header (correlation ID)
- `cache: 'no-store'`

| Endpoint | Method | Body | Response | Used By |
|---|---|---|---|---|
| `/desktop/index/file` | POST | `{ workspace, filename, content }` | `{ indexed, workspace, repo, filename, latency_ms }` | `LocalFiles` (Phase 2 watcher) |
| `/desktop/index/batch` | POST | `{ workspace, files: [{filename, content}] }` | `{ job_id, workspace, file_count }` | `WorkspaceRow.reindex()` |
| `/desktop/index/{workspace}` | DELETE | — | `{ deleted_chunks, workspace, repo }` | `WorkspaceRow.remove()` |
| `/desktop/index/{workspace}/status` | GET | — | `{ workspace, repo, chunk_count, last_indexed }` | `WorkspaceRow.loadStatus()` |
| `/desktop/register-mcp` | POST | `{ port, tools }` | `{ registered, port, tools }` | `LocalFiles` (Phase 5) |

### Frontend → Local MCP Server

| Endpoint | Method | Response | Used By |
|---|---|---|---|
| `http://127.0.0.1:{port}/tools` | GET | `{ tools: [...] }` | `LocalFiles` (Phase 5 — fetch tool list before registration) |

---

## Desktop Bridge Interface

The `useDesktop.js` hook acts as a bridge between the React renderer and the Electron main process. It detects the desktop environment via `window.ainxtDesktop?.isDesktop` and exposes the following functions used by this module:

| Function | Phase | Description |
|---|---|---|
| `isDesktop` | All | Boolean flag gating all desktop features |
| `pickFolder()` | 1 | Opens native folder picker dialog |
| `listFolder(dir, opts)` | 1 | Lists supported files in a directory (max 500) |
| `readFile(filePath)` | 1, 2 | Reads file content as UTF-8 text |
| `watchFolder(dir)` | 1 | Starts filesystem watcher on a folder |
| `unwatchFolder(dir)` | 1 | Stops filesystem watcher |
| `getWatchedFolders()` | 1 | Returns persisted list of watched folders |
| `onWorkspaceChange(cb)` / `offWorkspaceChange(cb)` | 2 | Subscribe/unsubscribe to file change events |
| `onClipboardChange(cb)` / `offClipboardChange(cb)` | 3 | Subscribe/unsubscribe to clipboard changes |
| `onShortcutContext(cb)` / `offShortcutContext(cb)` | 4 | Subscribe/unsubscribe to shortcut context events |
| `getMcpPort()` | 5 | Returns the port of the local MCP server |
| `onMcpServerReady(cb)` | 5 | Callback when local MCP server is ready |

> For full documentation of the desktop bridge, see [ai_ui_frontend_hooks](../ui/ai_ui_frontend_hooks.md).

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Not in desktop app | Renders fallback message: "Local Files requires the AiNxt desktop app." |
| Re-index finds no supported files | `WorkspaceRow` displays "No supported files found" error. |
| Re-index API call fails | `WorkspaceRow` displays the error message from the exception. |
| MCP server not started | `getMcpPort()` returns `null`; MCP status shows error with message. |
| MCP registration fails | Status bar shows yellow error with the HTTP status or exception message. |
| File change re-index fails | Silently caught (non-critical); no user-facing error. |
| Status fetch fails | `WorkspaceRow` shows "not indexed yet" (status remains `null`). |

---

## Security Considerations

- **Authentication:** All backend requests go through `authFetch`, which includes the user's httpOnly JWT cookie. The backend's `desktop_router.py` enforces `get_current_user` dependency on every endpoint.
- **MCP server verification:** The backend verifies the local MCP server is reachable at `127.0.0.1:{port}` before accepting registration, preventing registration of unreachable or spoofed servers.
- **MCP registry TTL:** Registered MCP entries expire after 8 hours (`_MCP_TTL = 28800`), requiring periodic re-registration.
- **File size limit:** Inline file indexing is capped at 512 KB (`MAX_FILE_SIZE = 524288`) to prevent oversized payloads.
- **Batch limit:** Batch indexing is limited to 500 files per request.
- **Workspace isolation:** Each workspace's vectors are stored under a repo name prefixed with `desktop_` and scoped to the user's department via RLS.

---

## Related Modules

| Module | Relationship |
|---|---|
| [ai_ui_frontend_hooks](../ui/ai_ui_frontend_hooks.md) | Provides the `useDesktop.js` Electron bridge used for all desktop interactions |
| [config](../infrastructure/config.md) | Provides `authFetch` for authenticated API calls |
| [desktop_router](../api/desktop_router.md) | Backend router handling all `/desktop/*` endpoints |
| [chat](../chat/chat.md) | Parent component that receives `onAskWithContext` prompts from clipboard and workspace actions |
| [sidebar](../sidebar.md) | Navigation entry point that renders the LocalFiles panel |
