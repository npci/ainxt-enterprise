# Auth Module

## Brief Introduction

The `auth` module provides lightweight, React Context-based session state management for the `ai-ui` frontend. It exposes a minimal API consisting of an `AuthProvider` component and a `useAuth` hook, allowing descendant components to read and update the currently authenticated user session without prop drilling.

> **Important:** The session state managed by this module is intentionally **not persisted** to `localStorage`, `sessionStorage`, or cookies. It is an in-memory-only context. Any persistence, token refresh, or logout orchestration is handled by consumers such as [`App.jsx`](../ui/ai_ui_frontend_app_core.md) and the backend [`auth_router`](../api/shared_api_routers.md#auth_router).

---

## Core Responsibilities

| Responsibility | Description |
| -------------- | ----------- |
| **Session State Container** | Holds the current user/session object in React state. |
| **Context Distribution** | Makes session and setter available to the entire React tree via context. |
| **Hook API** | Provides `useAuth()` for easy consumption in functional components. |
| **Non-Persistence Boundary** | Explicitly avoids persisting credentials; consumers decide storage/refresh strategy. |

---

## Architecture

### Component Overview

```mermaid
graph TD
    A[AuthProvider] -->|provides| B[AuthContext]
    C[useAuth Hook] -->|consumes| B
    D[App.jsx] -->|wraps| A
    E[Login.jsx] -->|calls setSession| A
    F[Any UI Component] -->|calls useAuth| C
```

### Module Placement

```mermaid
graph LR
    subgraph ai-ui Frontend
        direction TB
        App[App.jsx<br/>ai_ui_frontend_app_core] --> AuthProvider
        AuthProvider --> Login[Login.jsx<br/>login]
        AuthProvider --> Chat[Chat.jsx<br/>chat]
        AuthProvider --> Sidebar[Sidebar.jsx<br/>sidebar]
        AuthProvider --> Profile[Profile.jsx<br/>profile]
    end
    subgraph Backend Services
        AuthRouter[auth_router<br/>shared_api_routers]
        SSORouter[sso.py<br/>shared_core_authentication]
    end
    Login --> AuthRouter
    App --> SSORouter
```

---

## Core Components

### `AuthProvider`

A React component that creates the authentication context and maintains the `session` state.

```javascript
export function AuthProvider({ children }) {
  const [session, setSession] = useState(null);

  return (
    <AuthContext.Provider value={{ session, setSession }}>
      {children}
    </AuthContext.Provider>
  );
}
```

| Prop | Type | Description |
| ---- | ---- | ----------- |
| `children` | `ReactNode` | The React subtree that will have access to the auth context. |

| Context Value | Type | Description |
| ------------- | ---- | ----------- |
| `session` | `any \| null` | The current user/session object. `null` indicates no active session. |
| `setSession` | `function` | Setter to update or clear the session. |

### `useAuth`

A convenience hook that returns the current auth context value.

```javascript
export function useAuth() {
  return useContext(AuthContext);
}
```

| Return Value | Description |
| ------------ | ----------- |
| `{ session, setSession }` | The current session state and its updater. |

---

## Data Flow

### Login Flow

```mermaid
sequenceDiagram
    actor User
    participant Login as Login.jsx
    participant API as auth_router
    participant Provider as AuthProvider
    participant App as App.jsx

    User->>Login: Enters credentials / SSO
    Login->>API: POST /auth/login or /auth/sso_callback
    API-->>Login: Returns tokens/session payload
    Login->>Provider: setSession(payload)
    Provider-->>App: session updated via context
    App->>App: Re-renders authenticated layout
```

### Logout Flow

```mermaid
sequenceDiagram
    actor User
    participant App as App.jsx
    participant Provider as AuthProvider
    participant API as auth_router

    User->>App: Clicks logout
    App->>API: POST /auth/logout (optional)
    App->>Provider: setSession(null)
    Provider-->>App: session is null
    App->>App: Re-renders login screen
```

### Session Read Flow

```mermaid
sequenceDiagram
    participant Component as Any Component
    participant Hook as useAuth
    participant Context as AuthContext

    Component->>Hook: useAuth()
    Hook->>Context: useContext(AuthContext)
    Context-->>Hook: { session, setSession }
    Hook-->>Component: session object
```

---

## Component Interactions

| Consumer | Interaction |
| -------- | ----------- |
| [`App.jsx`](../ui/ai_ui_frontend_app_core.md) | Wraps the application with `AuthProvider`; uses `session` to choose between login and main UI; calls `setSession(null)` on logout. |
| [`Login.jsx`](../reference/login.md) | Authenticates the user and calls `setSession()` on success. |
| [`Profile.jsx`](../reference/profile.md) | Reads `session` to display user details. |
| [`Chat.jsx`](../chat/chat.md), [`KbChat.jsx`](../knowledge/kb_chat.md) | May read `session` to scope requests or display user-specific data. |
| [`config.js`](../ai_ui_frontend_config.md) | `authFetch` and `apiFetch` may attach tokens derived from the session object. |

---

## Dependencies

### Internal Dependencies

```mermaid
graph TD
    AuthContext[AuthContext.js] --> React[react]
    AuthContext --> App[App.jsx]
    AuthContext --> Login[Login.jsx]
    AuthContext --> Profile[Profile.jsx]
    AuthContext --> Chat[Chat.jsx]
```

### External Dependencies

| Package | Purpose |
| ------- | ------- |
| `react` | `createContext`, `useContext`, `useState` primitives. |

### Related Backend Modules

| Module | Relationship |
| ------ | ------------ |
| [`auth_router`](../api/shared_api_routers.md#auth_router) | Provides login, register, refresh, logout, SSO, and session management endpoints. |
| [`shared_core_authentication`](../reference/shared_core.md#authentication) | Contains RBAC, LDAP, and SSO logic used by the backend auth layer. |

---

## Process Flows

### How a Component Checks Authentication

```mermaid
flowchart TD
    A[Component renders] --> B{Called useAuth?}
    B -->|Yes| C[Read session from context]
    C --> D{session is null?}
    D -->|Yes| E[Render unauthenticated / redirect]
    D -->|No| F[Render authenticated content]
```

### How Session is Established

```mermaid
flowchart LR
    A[User action] --> B[Login or SSO handler]
    B --> C[Backend returns session]
    C --> D[setSession called]
    D --> E[Context value updated]
    E --> F[Subscribers re-render]
```

---

## Design Notes

1. **Intentionally Minimal:** The module does not implement token storage, refresh timers, or route guards. These concerns are delegated to consumers to keep the auth context reusable and framework-agnostic.
2. **No Persistence:** The comment `// NOT persisted` in the source is a deliberate security choice. Persisting tokens is the responsibility of callers (e.g., storing a refresh token in an `httpOnly` cookie or secure storage).
3. **No Default Value:** `createContext(null)` means components calling `useAuth()` outside `AuthProvider` will receive `null`. In practice, the entire app is wrapped by `AuthProvider` in [`App.jsx`](../ui/ai_ui_frontend_app_core.md).

---

## Usage Example

```jsx
import { useAuth } from "./AuthContext";

function UserGreeting() {
  const { session } = useAuth();

  if (!session) {
    return <p>Please log in.</p>;
  }

  return <p>Welcome, {session.name}!</p>;
}
```

```jsx
import { useAuth } from "./AuthContext";

function LoginButton({ onLogin }) {
  const { setSession } = useAuth();

  const handleLogin = async (credentials) => {
    const session = await onLogin(credentials);
    setSession(session);
  };

  return <button onClick={handleLogin}>Log In</button>;
}
```

---

## References

- [`ai_ui_frontend_app_core.md`](../ui/ai_ui_frontend_app_core.md) — Application root that wraps `AuthProvider` and handles logout.
- [`login.md`](../reference/login.md) — Login screen that populates the auth session.
- [`profile.md`](../reference/profile.md) — User profile that reads the session.
- [`ai_ui_frontend_config.md`](../ai_ui_frontend_config.md) — API fetch helpers that may use session tokens.
- [`shared_api_routers.md#auth_router`](../api/shared_api_routers.md#auth_router) — Backend authentication endpoints.
- [`shared_core.md#authentication`](../reference/shared_core.md#authentication) — Backend RBAC, LDAP, and SSO implementation.
