# code_conversations_router

## Brief Introduction

The `code_conversations_router` module provides a small, server-persisted REST API for the **Code tab** task/session history. Previously, Code tab sessions lived in browser `localStorage` and were lost on app restart. This router stores them durably in a dedicated PostgreSQL table (`code_conversations`), scoped to the authenticated user (`JWT sub`) and an optional project folder, enabling multi-device access and recovery across restarts.

The module intentionally keeps Code task sessions **separate** from cowork/buddy chat conversations (managed by [`cowork_conversations_router`](cowork_conversations_router.md)) to avoid mixing code-generation contexts with general chat history.

---

## Core Functionality

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/code/conversations` | `GET` | List the caller's Code sessions (metadata only), newest first. |
| `/code/conversations/{id}` | `GET` | Fetch a single session including its full message array. |
| `/code/conversations/{id}` | `PUT` | Create or update a session (title, folder, messages). Idempotent by `(id, user_id)`. |
| `/code/conversations/{id}` | `DELETE` | Delete a session owned by the caller. |

Key behaviors:

- **Schema-less messages**: the renderer's message-block array is stored verbatim as `JSONB`.
- **Lightweight metadata list**: listing returns only `id`, `title`, `folder`, and timestamps; messages are fetched on demand.
- **Size guardrail**: a single session's serialized messages are capped at ~4 MB to prevent runaway storage.
- **Self-healing schema**: the table and index are created idempotently on first access, so the Code tab works even if the global migration has not been run.

---

## Architecture

```mermaid
flowchart TB
    subgraph Client["Code Tab Frontend"]
        C[Renderer / IDE]
    end

    subgraph API["Shared API Layer"]
        R["code_conversations_router<br/>(FastAPI APIRouter)"]
    end

    subgraph Auth["Authentication"]
        U["get_current_user<br/>(JWT / API key / cookie)"]
    end

    subgraph Data["Data Layer"]
        E[(SQLAlchemy engine)]
        T[(code_conversations table)]
    end

    C -->|HTTP| R
    R -->|Depends| U
    R -->|raw SQL| E
    E --> T
```

The router is a standard FastAPI `APIRouter` mounted under `/code`. It does not use an ORM model; instead it executes raw SQL through the shared SQLAlchemy `engine` imported from [`db.database`](../db_database.md). Authentication is delegated to [`auth.dependencies.get_current_user`](../auth_dependencies.md), which accepts JWT bearer tokens, API keys, or `auth_token` cookies.

---

## Component Relationships

```mermaid
classDiagram
    class code_conversations_router {
        +APIRouter router
        +int _MAX_MESSAGES_BYTES
        +bool _table_ready
        +_ensure_table()
        +list_conversations()
        +get_conversation()
        +upsert_conversation()
        +delete_conversation()
    }

    class ConvUpsert {
        +str title
        +List[Any> messages
        +Optional[str] folder
    }

    class get_current_user {
        +dict current_user
    }

    class SQLAlchemyEngine {
        +engine
        +text()
    }

    class logger {
        +error()
    }

    code_conversations_router ..> ConvUpsert : uses as request body
    code_conversations_router ..> get_current_user : Depends
    code_conversations_router ..> SQLAlchemyEngine : executes raw SQL
    code_conversations_router ..> logger : logs table creation failures
```

### Components

- **`ConvUpsert`** — Pydantic request model for `PUT /code/conversations/{id}`. Carries `title`, `messages`, and optional `folder`.
- **`list_conversations`** — Returns metadata rows for the authenticated user, ordered by `created_at DESC`.
- **`get_conversation`** — Returns one session including its `messages` JSONB payload; returns HTTP 404 if not found.
- **`upsert_conversation`** — Inserts or updates a row using Postgres `ON CONFLICT (id, user_id) DO UPDATE`. Enforces the 4 MB message cap.
- **`delete_conversation`** — Deletes the row matching `(conv_id, user_id)` and reports whether anything was removed.
- **`_ensure_table`** — Idempotently creates the `code_conversations` table and a supporting index on first access.

---

## Data Flow

### Listing Sessions

```mermaid
sequenceDiagram
    participant C as Code Tab Client
    participant R as code_conversations_router
    participant A as get_current_user
    participant E as SQLAlchemy engine
    participant T as code_conversations

    C->>R: GET /code/conversations
    R->>A: validate token/cookie
    A-->>R: current_user["sub"]
    R->>R: _ensure_table()
    R->>E: SELECT metadata WHERE user_id = :uid
    E->>T: execute
    T-->>E: rows
    E-->>R: rows
    R-->>C: { conversations: [...] }
```

### Saving a Session

```mermaid
sequenceDiagram
    participant C as Code Tab Client
    participant R as code_conversations_router
    participant A as get_current_user
    participant E as SQLAlchemy engine
    participant T as code_conversations

    C->>R: PUT /code/conversations/{id}<br/>{title, folder, messages}
    R->>A: validate token/cookie
    A-->>R: current_user["sub"]
    R->>R: _ensure_table()
    R->>R: serialize messages & check size <= 4 MB
    R->>E: INSERT ... ON CONFLICT UPDATE
    E->>T: upsert row
    T-->>E: ok
    E-->>R: ok
    R-->>C: {id, saved: true}
```

### Deleting a Session

```mermaid
sequenceDiagram
    participant C as Code Tab Client
    participant R as code_conversations_router
    participant A as get_current_user
    participant E as SQLAlchemy engine
    participant T as code_conversations

    C->>R: DELETE /code/conversations/{id}
    R->>A: validate token/cookie
    A-->>R: current_user["sub"]
    R->>R: _ensure_table()
    R->>E: DELETE WHERE id = :id AND user_id = :uid
    E->>T: execute
    T-->>E: rowcount
    E-->>R: rowcount
    R-->>C: {deleted: true|false}
```

---

## Database Schema

```sql
CREATE TABLE IF NOT EXISTS code_conversations (
    id          TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    folder      TEXT,
    title       TEXT,
    messages    JSONB DEFAULT '[]'::jsonb,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_code_conv_user
    ON code_conversations (user_id, updated_at DESC);
```

- **Primary key** is composite `(id, user_id)`, so a user can only touch their own rows and session IDs need only be unique per user.
- **`folder`** allows the renderer to group sessions by project folder.
- **`messages`** is `JSONB` and stores the renderer's message-block array verbatim.
- **Index** supports fast per-user listing ordered by recency.

---

## How It Fits into the Overall System

The `code_conversations_router` sits in the **shared API routers** layer and is mounted into the main FastAPI application alongside many other domain routers (chat, agents, workflows, cowork, etc.). It is the persistence backend for the **Code tab** experience in the AI UI frontend and IDE integrations.

```mermaid
flowchart LR
    subgraph Frontend
        Code[Code Tab / IDE]
    end

    subgraph SharedRouters["Shared API Routers"]
        CCR["code_conversations_router"]
        CoCR["cowork_conversations_router"]
        CR["chat_router"]
        AR["agents_router"]
    end

    subgraph PlatformServices["Platform Services"]
        AuthS["auth.dependencies"]
        DB[(PostgreSQL)]
    end

    Code --> CCR
    Code -.->|separate concern| CoCR
    CCR --> AuthS
    CoCR --> AuthS
    CR --> AuthS
    AR --> AuthS
    CCR --> DB
    CoCR --> DB
    CR --> DB
    AR --> DB
```

It is deliberately **not** part of the cowork/chat conversation subsystem. The comment in the source explicitly calls this out: Code task sessions are kept in their own table so they never mix with Buddy chats. If you are looking for buddy/cowork conversation persistence, see [`cowork_conversations_router`](cowork_conversations_router.md).

---

## Dependencies

| Dependency | Module | Role |
|------------|--------|------|
| `get_current_user` | [`auth.dependencies`](../auth_dependencies.md) | Resolves JWT, API key, or cookie into a user dict containing `sub`. |
| `engine` | [`db.database`](../db_database.md) | Shared SQLAlchemy engine used to run raw SQL. |
| `logger` | [`core.logger`](../core_logger.md) | Logs failures during idempotent table creation. |

---

## Error Handling & Guardrails

- **401 Unauthorized**: returned by `get_current_user` for missing/invalid tokens.
- **404 Not Found**: returned by `get_conversation` when no row matches `(conv_id, user_id)`.
- **413 Payload Too Large**: returned by `upsert_conversation` when serialized messages exceed `_MAX_MESSAGES_BYTES` (~4 MB).
- **500 Internal Server Error**: returned if the upsert SQL execution fails; the exception string is surfaced in the detail.
- **Table creation failures** are caught and logged rather than raised, so a transient DDL issue does not crash the request path (subsequent SQL may still fail, but the error is explicit).

---

## Related Modules

- [`cowork_conversations_router`](cowork_conversations_router.md) — similar API shape but for cowork/buddy chat sessions; intentionally separate data and table.
- [`chat_router`](chat_router.md) — general chat history, artifacts, and message management.
- [`auth_dependencies`](../auth_dependencies.md) — authentication/authorization resolution.
- [`db_database`](../db_database.md) — database engine and session management.
- [`core_logger`](../core_logger.md) — structured logging utilities.
