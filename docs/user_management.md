# User Management Module

## Brief Introduction

The **User Management** module provides the administrative interface for viewing, creating, and modifying platform user accounts within the `ai-ui` frontend. It is implemented as a single React component, `UserManagement`, and is gated behind role-based access so that only users with `security` or `admin` privileges can manage accounts. The module supports listing registered users, editing user roles, enabling/disabling accounts, and creating new users with an initial temporary password.

This module is part of the broader [ai_ui_frontend](ai_ui_frontend.md) application and consumes the [auth_router](auth_router.md) backend endpoints for user lifecycle operations.

---

## Core Functionality

### 1. User Listing

The component loads the full list of registered users from the backend on mount:

- **Endpoint:** `GET {API_BASE}/auth/users`
- **Response:** `{ users: [...], total: <number> }`
- **Displayed fields:** name, email, role, active/disabled status, and registration date (converted to IST).

### 2. Role Management

Administrators can change a user's role via a modal dialog. Supported roles are:

| Role       | Description                              |
|------------|------------------------------------------|
| `viewer`   | Read-only access                         |
| `developer`| Can build and run agents/workflows       |
| `operator` | Operational control and monitoring       |
| `security` | Security and user administration         |
| `admin`    | Full platform administration             |

- **Endpoint:** `PATCH {API_BASE}/auth/users/{userId}`
- **Payload:** `{ role: "<role>" }`

### 3. Account Enable/Disable

Administrators can toggle a user's active state:

- **Endpoint:** `PATCH {API_BASE}/auth/users/{userId}`
- **Payload:** `{ is_active: <boolean> }`

### 4. User Creation

Administrators can create new users with a temporary password:

- **Endpoint:** `POST {API_BASE}/auth/users`
- **Payload:** `{ name, email, password, role }`

---

## Architecture

### Component Structure

```mermaid
graph TD
    A[UserManagement] --> B[usePermission hook]
    A --> C[authFetch helper]
    A --> D[toISTDate utility]
    A --> E[Auth API /auth/users]

    B --> F{isSecurity?}
    F -->|No| G[Access denied message]
    F -->|Yes| H[Load users table]

    H --> I[Edit role modal]
    H --> J[Create user modal]
    H --> K[Enable/Disable toggle]
```

### State Management

```mermaid
stateDiagram-v2
    [*] --> Loading
    Loading --> EmptyOrListed : fetch success
    Loading --> Error : fetch failed
    EmptyOrListed --> Editing : open edit role modal
    EmptyOrListed --> Creating : open create user modal
    Editing --> EmptyOrListed : save/cancel
    Creating --> EmptyOrListed : create/cancel
    EmptyOrListed --> EmptyOrListed : enable/disable user
```

---

## Dependencies

### Frontend Dependencies

| Dependency | Module | Purpose |
|------------|--------|---------|
| `usePermission` | [ai_ui_frontend_hooks](ai_ui_frontend_hooks.md) | Role-based access check (`isAdmin`, `isSecurity`) |
| `authFetch` | [ai_ui_frontend_config](ai_ui_frontend_config.md) | Authenticated HTTP requests |
| `toISTDate` | [ai_ui_frontend_utils](ai_ui_frontend_utils.md) | Date formatting to IST timezone |

### Backend Dependencies

| Dependency | Module | Purpose |
|------------|--------|---------|
| `GET /auth/users` | [auth_router](auth_router.md) | List all users |
| `POST /auth/users` | [auth_router](auth_router.md) | Create a new user |
| `PATCH /auth/users/{id}` | [auth_router](auth_router.md) | Update role or active status |

---

## Data Flow

```mermaid
sequenceDiagram
    actor Admin
    participant UserManagement
    participant usePermission
    participant authFetch
    participant AuthAPI as auth_router

    Admin->>UserManagement: Open Users page
    UserManagement->>usePermission: Check isSecurity / isAdmin
    usePermission-->>UserManagement: Access granted
    UserManagement->>authFetch: GET /auth/users
    authFetch->>AuthAPI: Authenticated request
    AuthAPI-->>authFetch: { users, total }
    authFetch-->>UserManagement: User list
    UserManagement-->>Admin: Render user table

    alt Edit Role
        Admin->>UserManagement: Click "Edit role"
        UserManagement->>UserManagement: Show edit modal
        Admin->>UserManagement: Select new role & save
        UserManagement->>authFetch: PATCH /auth/users/{id}
        authFetch->>AuthAPI: Update role
        AuthAPI-->>authFetch: Success
        UserManagement->>authFetch: Reload users
    end

    alt Enable/Disable
        Admin->>UserManagement: Toggle active status
        UserManagement->>authFetch: PATCH /auth/users/{id}
        authFetch->>AuthAPI: Update is_active
        AuthAPI-->>authFetch: Success
        UserManagement->>authFetch: Reload users
    end

    alt Create User
        Admin->>UserManagement: Click "+ New User"
        UserManagement->>UserManagement: Show create modal
        Admin->>UserManagement: Fill form & create
        UserManagement->>authFetch: POST /auth/users
        authFetch->>AuthAPI: Create user
        AuthAPI-->>authFetch: Success
        UserManagement->>authFetch: Reload users
    end
```

---

## Access Control

The module enforces two levels of access:

1. **Security/Admin gate:** The entire page is hidden unless `isSecurity` is true. This is checked via the `usePermission` hook.
2. **Admin-only actions:** The "New User" button and the "Actions" column (edit role, enable/disable) are only rendered when `isAdmin` is true.

```mermaid
flowchart LR
    A[Current User] --> B{usePermission}
    B -->|isSecurity| C[View Users Page]
    B -->|Not isSecurity| D[Show Access Denied]
    C --> E{isAdmin?}
    E -->|Yes| F[Show Create + Edit + Toggle]
    E -->|No| G[View Only]
```

---

## UI Layout

```mermaid
graph TD
    A[Users Page] --> B[Header]
    A --> C[Error Banner]
    A --> D[Users Table]
    A --> E[Edit Role Modal]
    A --> F[Create User Modal]

    B --> B1[Title + Total count]
    B --> B2[+ New User button admin only]

    D --> D1[Name]
    D --> D2[Email]
    D --> D3[Role badge]
    D --> D4[Status badge]
    D --> D5[Joined date]
    D --> D6[Actions admin only]

    E --> E1[Role dropdown]
    E --> E2[Save / Cancel]

    F --> F1[Full Name input]
    F --> F2[Email input]
    F --> F3[Password input]
    F --> F4[Role select]
    F --> F5[Create / Cancel]
```

---

## Error Handling

- **Load failures:** Displayed in the error banner at the top of the page.
- **Update/create failures:** Backend error detail is shown inline in the relevant modal or banner.
- **Unauthorized access:** A centered message informs the user that the page requires a Security or Admin role.

---

## Integration with the Overall System

The User Management module is one of several administrative features in the `ai-ui` frontend. It integrates with the platform's authentication and authorization layer:

- **Authentication:** Uses `authFetch` to include the current session token in all requests.
- **Authorization:** Relies on the backend's `auth_router` to enforce admin/security checks server-side, even though the UI also gates actions.
- **RBAC:** The roles managed here (`viewer`, `developer`, `operator`, `security`, `admin`) align with the platform-wide role-based access control implemented in [auth_rbac](auth_rbac.md).

For related administrative interfaces, see:

- [Budget Manager](budget_manager.md) — budget allocation and approvals
- [Model Governance](model_governance.md) — model access permissions
- [Level Overrides](level_overrides.md) — temporary permission elevation
- [Endpoint Manager](endpoint_manager.md) — API key and endpoint management

---

## File Reference

| File | Component | Responsibility |
|------|-----------|--------------|
| `ai-ui/src/components/UserManagement.jsx` | `UserManagement` | Main user administration UI |
