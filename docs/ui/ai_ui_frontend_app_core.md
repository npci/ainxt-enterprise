# AI-UI Frontend App Core

## 1. Introduction

The `ai_ui_frontend_app_core` module is the **root application shell** of the AI-UI (AiNxt Enterprise) frontend. It is the single entry point that bootstraps the entire single-page application (SPA), managing authentication, routing, global state, and the layout skeleton (sidebar + content area). Every feature view in the platform — Chat, Agents, Knowledge Base, SDLC Pipeline, Budget Manager, and dozens more — is mounted, unmounted, and kept alive through this module.

At its core, `App.jsx` is a React functional component that:

- **Authenticates** users via server-side httpOnly cookies (never localStorage), rendering a `Login` screen until `/auth/me` confirms a valid session.
- **Routes** between 30+ feature views using React Router, with URL-path ↔ view-key bidirectional mapping.
- **Hoists shared state** (chat list, project messages, inbox unread count) so streaming responses survive route navigation.
- **Preserves the Office (Buddy) component** via a CSS keep-alive pattern, preventing the loss of in-flight CLI streamed tokens when users switch tabs.
- **Wraps every route** in an `ErrorBoundary` and provides global `Toast` and `Confirm` dialog contexts.

---

## 2. Architecture Overview

```mermaid
graph TB
    subgraph AppShell["App.jsx — Root Shell"]
        AuthGate["Auth Gate<br/>(authChecked + user)"]
        Router["React Router<br/>(Routes / Route)"]
        SidebarComp["Sidebar"]
        ContentArea["Content Area"]
        OfficeKeepAlive["Office Keep-Alive<br/>(CSS toggle)"]
    end

    subgraph AuthFlow["Authentication Flow"]
        AuthMe["GET /auth/me<br/>(httpOnly cookie)"]
        LoginComp["Login Component"]
        HandleAuth["handleAuth()"]
        HandleLogout["handleLogout()"]
    end

    subgraph GlobalState["Hoisted Global State"]
        ChatState["Chat State<br/>chats, activeChatId, chatsLoading"]
        ProjectState["Project State<br/>projectMessages, projectLoading<br/>activeProjectId, projectAbortRef"]
        InboxState["Inbox State<br/>unreadCount"]
        NavState["Nav State<br/>navCollapsed, refreshKey"]
    end

    subgraph Providers["Global Providers"]
        ToastProv["ToastProvider"]
        ConfirmProv["ConfirmProvider"]
    end

    AuthGate -->|not authenticated| LoginComp
    LoginComp -->|onAuth| HandleAuth
    HandleAuth --> AuthGate
    AuthGate -->|authenticated| Router
    AuthMe --> AuthGate
    AuthGate --> SidebarComp
    AuthGate --> ContentArea
    ContentArea --> Router
    ContentArea --> OfficeKeepAlive
    Router --> ChatState
    Router --> ProjectState
    SidebarComp --> InboxState
    SidebarComp --> NavState
    ToastProv --> AppShell
    ConfirmProv --> AppShell
    HandleLogout --> AuthMe
```

---

## 3. Core Components

### 3.1 `App` — Root Component

The `App` component is the top-level React component exported as default from `App.jsx`. It orchestrates the entire application lifecycle.

**Key responsibilities:**

| Responsibility | Mechanism |
|---|---|
| Session restoration | `useEffect` calls `GET /auth/me` on mount; sets `user` state from response |
| Authentication gate | Renders `Login` until `authChecked` is `true` and `user` is non-null |
| View routing | Derives `view` from `location.pathname`; `setView()` navigates via `useNavigate` |
| Chat list management | Fetches `/chats` on login (deferred 500ms); maps backend response to local chat objects |
| Project state hoisting | Maintains `projectMessages`, `projectLoading`, `activeProjectId`, `projectAbortRef` |
| Inbox polling | Polls `/inbox/unread-count` every 5 minutes (deferred 3s after login) |
| Tab-visibility refresh | Remounts non-chat views after 10+ minutes of tab inactivity via `refreshKey` |
| Sidebar persistence | `navCollapsed` state persisted to `localStorage` |
| Office keep-alive | Renders `<Office>` outside `<Routes>` with CSS `hidden`/`flex` toggle |

**State diagram:**

```mermaid
stateDiagram-v2
    [*] --> Loading: Mount
    Loading --> Unauthenticated: /auth/me returns null
    Loading --> Authenticated: /auth/me returns user data
    Unauthenticated --> Authenticated: Login → handleAuth()
    Authenticated --> Unauthenticated: handleLogout()
    Authenticated --> Authenticated: Route navigation (setView)
    Authenticated --> Authenticated: Tab refocus after 10min (refreshKey++)
```

### 3.2 `handleAuth(authData)`

Called by the `Login` component after a successful server-side session verification. Sets the `user` state and marks `authChecked` as `true`, causing the app to transition from the login screen to the main shell.

```javascript
function handleAuth(authData) {
  setUser(authData);
  setAuthChecked(true);
}
```

**User object shape:**
```javascript
{
  userId, email, name, role,
  ad_level,           // Access-control level (default 6)
  department,
  can_approve,
  is_hod,
  hod_departments,
  is_reporting_manager
}
```

### 3.3 `handleLogout()`

Performs a full session teardown:

1. Sends `POST /auth/logout` to invalidate the server-side session.
2. Calls `coworkOfficeClearKey()` to wipe the desktop-persisted CLI API key and Entra refresh token (no-op on web).
3. Clears local chat state (`chats`, `activeChatId`).
4. Resets `user` to `null` and navigates to `/`.
5. Normalizes the URL to `${PORTAL_BASE}/` for hard-reload compatibility.

```mermaid
sequenceDiagram
    participant U as User
    participant App as App.jsx
    participant API as Backend API
    participant Desktop as Desktop Bridge

    U->>App: Click "Sign out"
    App->>API: POST /auth/logout
    App->>Desktop: coworkOfficeClearKey()
    App->>App: setChats([]), setActiveChatId(null)
    App->>App: setUser(null)
    App->>App: navigate("/", { replace: true })
    App->>App: Normalize URL to PORTAL_BASE/
    App-->>U: Render Login screen
```

### 3.4 `onVisibilityChange()`

An event listener attached to `document.visibilitychange`. When the tab becomes visible again after being hidden, it checks the elapsed time since the last refresh:

- **< 10 minutes**: No action — preserves in-progress form data during short tab switches.
- **≥ 10 minutes**: Increments `refreshKey`, which causes non-chat views (those keyed with `refreshKey`) to remount and fetch fresh data.

This prevents stale data after long absences while avoiding unnecessary state destruction during quick alt-tab switches.

### 3.5 `toggleNav()`

Toggles the sidebar between collapsed (14px wide) and expanded (56px wide) states. The preference is persisted to `localStorage` under the key `nav_collapsed`.

---

## 4. Routing Architecture

The app uses React Router v6 with a bidirectional URL-path ↔ view-key mapping. This enables deep-linking and browser back/forward navigation.

```mermaid
graph LR
    subgraph PathToView["PATH_TO_VIEW (URL → View)"]
        P1["/chat"] --> V1["chat"]
        P2["/agents"] --> V2["agents"]
        P3["/sdlc"] --> V3["sdlc"]
        P4["/budget-manager"] --> V4["budget"]
        P5["/office"] --> V5["office"]
        PN["...30+ paths"] --> VN["...30+ views"]
    end

    subgraph ViewToPath["VIEW_TO_PATH (View → URL)"]
        V1 --> P1
        V2 --> P2
        V3 --> P3
        V4 --> P4
        V5 --> P5
    end

    Browser["Browser URL"] --> PathToView
    ViewToPath --> Browser
```

**Route rendering strategy:**

| Pattern | Views | Behavior |
|---|---|---|
| **Stable key** (no `refreshKey`) | `chat`, `products`, `build-studio`, `office` | Never remounted on tab refocus; preserves form/streaming state |
| **`refreshKey`-suffixed key** | `agents`, `sdlc`, `budget`, `monitoring`, `analytics`, etc. | Remounted after 10+ min tab inactivity to fetch fresh data |
| **Keep-alive (outside Routes)** | `office` | Rendered once; toggled with CSS `hidden`/`flex` to preserve CLI event listeners |
| **Fallback** | `*` | Redirects to `/chat` |

### Office Keep-Alive Pattern

The `Office` (Buddy) component is deliberately rendered **outside** the `<Routes>` block. This is a critical architectural decision:

```mermaid
graph TD
    subgraph Problem["Problem (if Office were a Route)"]
        A1["User on /office, CLI streaming answer"] --> A2["User switches to /chat"]
        A2 --> A3["Route unmounts Office"]
        A3 --> A4["coworkOffice:onEvent listener destroyed"]
        A4 --> A5["Streamed tokens lost"]
        A5 --> A6["Turn surfaces as 'error' / '0 tok'"]
    end

    subgraph Solution["Solution (CSS Keep-Alive)"]
        B1["User on /office, CLI streaming answer"] --> B2["User switches to /chat"]
        B2 --> B3["Office stays mounted (CSS hidden)"]
        B3 --> B4["coworkOffice:onEvent listener alive"]
        B4 --> B5["Tokens keep streaming"]
        B5 --> B6["User returns — answer is there"]
    end
```

---

## 5. Authentication & Security

### 5.1 Session Model

The application uses a **cookie-based, server-validated** authentication model. No tokens or user data are stored in `localStorage` or `sessionStorage`.

```mermaid
sequenceDiagram
    participant Browser
    participant Login as Login.jsx
    participant Gateway as Backend Gateway
    participant AuthAPI as /auth/me

    Browser->>Login: Enter credentials + CAPTCHA
    Login->>Gateway: POST /auth/login {email, encrypted password}
    Gateway-->>Login: 200 OK (sets httpOnly cookie)
    Login->>AuthAPI: GET /auth/me (cookie sent automatically)
    AuthAPI-->>Login: 200 {id, email, name, role, ...}
    Login->>Login: onAuth(meData) → handleAuth()
    Note over Login,AuthAPI: Security: auth decision based on<br/>httpOnly cookie, NOT response body.<br/>Prevents login bypass via response manipulation.

    Note over Browser,AuthAPI: On page reload:
    Browser->>AuthAPI: GET /auth/me (cookie sent)
    AuthAPI-->>Browser: 200 {user data} or 401
```

### 5.2 `authFetch` — Authenticated Request Helper

All API calls from the app shell use `authFetch` from the [config](../infrastructure/config.md) module, which:

- Sets `credentials: 'include'` to send httpOnly cookies.
- Adds `x-client-request-id` header for tracing.
- Retries idempotent (GET/HEAD) requests once after a 400ms backoff on network failure.
- Uses `cache: 'no-store'` to prevent stale responses.

> **See also:** [config](../infrastructure/config.md) module for `authFetch` and `apiFetch` implementation details.

### 5.3 Access Control

User visibility of sidebar items is controlled by `ad_level` (Active Directory level) and `role`. The `Sidebar` component uses a `canSee(maxLevel)` function: items with `maxLevel >= user.ad_level` (or admin role) are shown. Some items also have server-side allowlist probes (e.g., `/broadcast/access`, `/tenx/access`).

> **See also:** [sidebar](../sidebar.md) module for nav group configuration and visibility logic.

---

## 6. Global State Management

The `App` component hoists several pieces of shared state that must survive route navigation:

```mermaid
graph TB
    subgraph AppLevelState["App-Level State (hoisted)"]
        ChatList["chats: Chat[]<br/>activeChatId: string<br/>chatsLoading: boolean"]
        ProjMsgs["projectMessages: {[id]: Message[]}<br/>projectLoading: {[id]: boolean}<br/>activeProjectId: string<br/>projectAbortRef: AbortController"]
        Inbox["unreadCount: number"]
        Nav["navCollapsed: boolean<br/>refreshKey: number"]
        User["user: User | null<br/>authChecked: boolean"]
    end

    ChatList -->|props| ChatView["Chat Component"]
    ProjMsgs -->|props| ProjectsView["Projects Component"]
    Inbox -->|props| SidebarComp["Sidebar Component"]
    Nav -->|props| SidebarComp
    User -->|props| AllViews["All Views"]
```

### 6.1 Chat State

Chat data is fetched from the database (not localStorage) on login. The fetch is deferred 500ms to prioritize initial UI rendering. Each chat object includes KB-chat metadata (`rag_mode`, `product_id`, `domain`, `spec_version`, `kb_doc_id`) to ensure KB chat history persists across page refreshes.

### 6.2 Project State

Project messages and loading state are keyed by project ID, allowing each workspace to have independent streaming state. An `AbortController` ref (`projectAbortRef`) enables canceling in-flight streams when switching projects.

### 6.3 Inbox Polling

Inbox unread count is polled every 5 minutes, with the first poll deferred 3 seconds after login to avoid competing with the critical rendering path.

---

## 7. Dependency Map

```mermaid
graph TD
    App["App.jsx<br/>(ai_ui_frontend_app_core)"]

    App -->|imports| Config["config.js<br/>authFetch, apiFetch, API_BASE, PORTAL_BASE"]
    App -->|imports| UseDesktop["useDesktop.js<br/>coworkOfficeClearKey"]
    App -->|imports| Login["Login.jsx"]
    App -->|imports| Sidebar["Sidebar.jsx"]
    App -->|imports| DialogProvider["DialogProvider.jsx<br/>ToastProvider, ConfirmProvider"]
    App -->|imports| ErrorBoundary["ErrorBoundary.jsx"]

    App -->|routes to| Chat["Chat.jsx"]
    App -->|routes to| AgentsCatalog["AgentsCatalog.jsx"]
    App -->|routes to| SDLCPipeline["SDLCPipeline.jsx"]
    App -->|routes to| BudgetManager["BudgetManager.jsx"]
    App -->|routes to| KnowledgeBase["KnowledgeBase.jsx"]
    App -->|routes to| Office["Office.jsx<br/>(keep-alive)"]
    App -->|routes to| MoreViews["...25+ other views"]

    Config -->|HTTP| Gateway["Backend Gateway<br/>(gateway module)"]
    Login -->|POST /auth/login| Gateway
    App -->|GET /auth/me| Gateway
    App -->|GET /chats| Gateway
    App -->|GET /inbox/unread-count| Gateway

    UseDesktop -->|IPC| DesktopApp["Desktop App<br/>(desktop_app module)"]
```

### Key Dependencies

| Dependency | Module | Purpose |
|---|---|---|
| `authFetch`, `apiFetch` | [config](../infrastructure/config.md) | Authenticated HTTP requests with retry and correlation IDs |
| `coworkOfficeClearKey` | [ai_ui_frontend_hooks](ai_ui_frontend_hooks.md) | Desktop bridge — clears OS-persisted CLI keys on logout |
| `Login` | [login](../reference/login.md) | Authentication screen with CAPTCHA and server-side session verification |
| `Sidebar` | [sidebar](../sidebar.md) | Navigation sidebar with role-based visibility, budget widget, SDLC pulse |
| `ToastProvider`, `ConfirmProvider` | [ui_dialog](ui_dialog.md) | Global toast notifications and confirmation dialogs |
| `ErrorBoundary` | — | Per-route error isolation preventing full-app crashes |
| `Chat` | [chat](../chat/chat.md) | Main chat interface (receives hoisted chat state) |
| `Office` | — | Buddy/Office component (keep-alive pattern) |
| `Projects` | [projects](../reference/projects.md) | Project workspace (receives hoisted project state) |

---

## 8. View Registry

The following table lists all registered routes and their corresponding feature modules:

| URL Path | View Key | Component | Module Reference |
|---|---|---|---|
| `/chat` | `chat` | `Chat` | [chat](../chat/chat.md) |
| `/agents` | `agents` | `AgentsCatalog` | [agents_catalog](../agents/agents_catalog.md) |
| `/knowledge` | `knowledge` | `KnowledgeBase` | [knowledge_base](../knowledge/knowledge_base.md) |
| `/graph` | `graph` | `KnowledgeGraph` | [knowledge_graph](../knowledge/knowledge_graph.md) |
| `/products` | `products` | `ProductManager` | [product_manager](../reference/product_manager.md) |
| `/codebase` | `codebase` | `CodebaseManager` | [codebase_manager](../reference/codebase_manager.md) |
| `/projects` | `projects` | `Projects` | [projects](../reference/projects.md) |
| `/threads` | `threads` | `Threads` | [threads](../chat/threads.md) |
| `/discussions` | `discussions` | `Discussions` | [discussions](../chat/discussions.md) |
| `/inbox` | `inbox` | `Inbox` | [inbox](../chat/inbox.md) |
| `/sdlc` | `sdlc` | `SDLCPipeline` | [sdlc_pipeline](../sdlc/sdlc_pipeline.md) |
| `/build-studio` | `build-studio` | `BuildStudio` | [ai_ui_frontend_build_studio](ai_ui_frontend_build_studio.md) |
| `/monitoring` | `monitoring` | `Monitoring` | [monitoring](../infrastructure/monitoring.md) |
| `/analytics` | `analytics` | `AgentAnalytics` | [agent_analytics](../agents/agent_analytics.md) |
| `/evals` | `evals` | `EvalsDashboard` | [evals_dashboard](../evaluation/evals_dashboard.md) |
| `/model-governance` | `model-governance` | `ModelGovernance` | [model_governance](../sdlc/model_governance.md) |
| `/budget-manager` | `budget` | `BudgetManager` | [budget_manager](../models/budget_manager.md) |
| `/level-overrides` | `level-overrides` | `LevelOverrides` | [level_overrides](../clients/level_overrides.md) |
| `/broadcast` | `broadcast` | `EmailBroadcast` | [email_broadcast](../chat/email_broadcast.md) |
| `/endpoint-manager` | `endpoint-manager` | `EndpointManager` | [endpoint_manager](../reference/endpoint_manager.md) |
| `/dept-metrics` | `dept-metrics` | `DeptMetrics` | [dept_metrics](../infrastructure/dept_metrics.md) |
| `/memory` | `memory` | `Memory` | [memory](../reference/memory.md) |
| `/connectors` | `connectors` | `Connectors` | [connectors](../connectors/connectors.md) |
| `/cowork-setup` | `cowork-setup` | `CoworkSettings` | [cowork_settings](../buddy/cowork_settings.md) |
| `/office` | `office` | `Office` (keep-alive) | — |
| `/code` | `cowork` | `Code` | [code](../reference/code.md) |
| `/documents` | `docs` | `DocsPanel` | [documents](../documents/documents.md) |
| `/profile` | `profile` | `Profile` | [profile](../reference/profile.md) |
| `/tenx` | `tenx` | `TenXAward` | — |
| `/coach` | `coach` | `Coach` | [coach](../evaluation/coach.md) |
| `/skill-proposals` | `skill-proposals` | `SkillProposals` | [skill_proposals](../agents/skill_proposals.md) |

---

## 9. Application Lifecycle

```mermaid
sequenceDiagram
    participant User
    participant App as App.jsx
    participant API as Backend API
    participant Router as React Router

    Note over App: ── Phase 1: Boot ──
    App->>App: Mount, authChecked = false
    App->>API: GET /auth/me (httpOnly cookie)
    App->>App: Render null (avoid login flash)

    Note over App: ── Phase 2: Auth Resolution ──
    alt Session valid
        API-->>App: 200 {id, email, name, role, ...}
        App->>App: setUser(data), authChecked = true
    else No session
        API-->>App: 401 / null
        App->>App: authChecked = true, user = null
        App->>App: Render <Login>
    end

    Note over App: ── Phase 3: Post-Login Init ──
    App->>App: Render Sidebar + Routes
    App->>API: GET /chats (deferred 500ms)
    App->>API: GET /inbox/unread-count (deferred 3s)

    Note over App: ── Phase 4: Steady State ──
    User->>Router: Navigate to /sdlc
    Router->>App: location.pathname = "/sdlc"
    App->>App: view = "sdlc"
    App->>App: Render <SDLCPipeline> in <ErrorBoundary>

    Note over App: ── Phase 5: Tab Refocus ──
    User->>App: Switch away (tab hidden)
    User->>App: Return after 12 minutes
    App->>App: onVisibilityChange fires
    App->>App: now - lastRefreshAt > 10min
    App->>App: refreshKey++ → non-chat views remount
```

---

## 10. Error Handling

Every route is wrapped in an `ErrorBoundary` component with a unique key. This ensures:

1. **Isolation**: A crash in one view (e.g., `SDLCPipeline`) does not bring down the entire application.
2. **Recovery**: Users can navigate away from a crashed view and return to a fresh mount.
3. **Refresh compatibility**: Views keyed with `refreshKey` get a new `ErrorBoundary` instance on tab refocus, clearing any error state along with the remount.

The `ToastProvider` and `ConfirmProvider` wrap the entire app shell, providing imperative APIs (`toast.success()`, `toast.error()`, `confirm()`) accessible to all child components via React context.

> **See also:** [ui_dialog](ui_dialog.md) for `ToastProvider` and `ConfirmProvider` implementation details.

---

## 11. Backend Integration Points

The `App` component directly calls the following backend endpoints:

| Endpoint | Method | Purpose | Timing |
|---|---|---|---|
| `/auth/me` | GET | Session restoration on mount | On mount |
| `/auth/logout` | POST | Session invalidation | On logout |
| `/chats` | GET | Fetch chat list from DB | 500ms after login |
| `/inbox/unread-count` | GET | Poll inbox unread count | 3s after login, then every 5 min |

All other API calls are made by individual feature components (e.g., `Chat`, `SDLCPipeline`, `BudgetManager`) and are documented in their respective module pages.

> **See also:** [gateway](../models/gateway.md) module for the backend API gateway that serves these endpoints.

---

## 12. Desktop Integration

When running inside the Electron desktop app, the `App` component integrates with the desktop bridge via `coworkOfficeClearKey()` from the [useDesktop](ai_ui_frontend_hooks.md) hook. On logout, this clears:

- The OS-persisted CLI API key
- The Entra (Azure AD) refresh token

This ensures that on shared machines, the next user cannot inherit the previous user's session. The call is a no-op when running in a web browser (the function returns `{ ok: false }` if `window.ainxtDesktop` is not present).

> **See also:** [desktop_app](../clients/desktop_app.md) module for the Electron desktop application and IPC bridge.
