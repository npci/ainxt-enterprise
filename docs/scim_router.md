# SCIM Router

## Brief Introduction

The `scim_router` module exposes a **SCIM 2.0** (System for Cross-domain Identity Management) provisioning surface at `/scim/v2`. It allows enterprise Identity Providers (IdPs) such as Okta, Azure AD, Ping, or OneLogin to automatically provision, update, and de-provision users in the AiNxt / Claude Cowork platform.

SCIM users are SSO-provisioned: they have no local password and authenticate through the configured SSO provider. The router supports full CRUD on `User` resources and read-only discovery of `Group` resources derived from the `department` column on the `User` table.

---

## Core Functionality

### Supported SCIM Endpoints

| Method | Path | Handler | Purpose |
|--------|------|---------|---------|
| GET | `/scim/v2/ServiceProviderConfig` | `service_provider_config` | SCIM discovery: capabilities, auth scheme |
| GET | `/scim/v2/ResourceTypes` | `resource_types` | Lists supported resource types (`User`, `Group`) |
| GET | `/scim/v2/Users` | `list_users` | List users with pagination and basic `userName eq` filter |
| GET | `/scim/v2/Users/{user_id}` | `get_user` | Retrieve a single user |
| POST | `/scim/v2/Users` | `create_user` | Provision a new user |
| PUT | `/scim/v2/Users/{user_id}` | `replace_user` | Full replacement of a user record |
| PATCH | `/scim/v2/Users/{user_id}` | `patch_user` | Partial update, commonly used for activate/deactivate |
| DELETE | `/scim/v2/Users/{user_id}` | `delete_user` | Soft-deactivate a user (de-provision) |
| GET | `/scim/v2/Groups` | `list_groups` | Read-only groups derived from `User.department` |

### Security Model

- **Bearer token authentication** using a dedicated `SCIM_TOKEN` environment variable.
- If `SCIM_TOKEN` is unset, the entire surface returns **503 Service Unavailable**.
- Token comparison uses `hmac.compare_digest` to prevent timing attacks.
- The token is **never logged**.
- This is an automation/admin surface and does **not** use the user JWT path. For user-facing authentication and SSO flows, see [auth_router](auth_router.md).

### User Lifecycle Behavior

- **Create**: Inserts a `User` row with `hashed_password=None`, `sso_provider` from `SSO_PROVIDER` env (default `"scim"`), and `sso_subject` from `externalId` or email.
- **Duplicate email**: Returns **409 Conflict** with the existing user resource. If the existing user is inactive, it is reactivated.
- **Replace (PUT)**: Updates email, name, active status, and department.
- **Patch (PATCH)**: Supports `replace`/`add` operations for `active`, `displayName`, and object-style `value` blocks.
- **Delete**: Performs a **soft deactivation** (`is_active=False`, `account_status="suspended"`) for audit and data-retention compliance.

### Group Model

Groups are read-only and derived from distinct non-null `User.department` values. Membership is inferred from `User.department`; the endpoint does not expand individual members.

---

## Architecture

```mermaid
flowchart TB
    subgraph IdP["Enterprise IdP (Okta / Azure AD / Ping / OneLogin)"]
        direction TB
        PROV["Provisioning requests"]
    end

    subgraph FastAPI["FastAPI Application"]
        direction TB
        ROUTER["/scim/v2 router<br/>routers/scim_router.py"]
        AUTH["_require_scim_token<br/>Bearer token gate"]
    end

    subgraph Data["Data Layer"]
        direction TB
        DB[("PostgreSQL<br/>db.models.User")]
        ENV[("Environment<br/>SCIM_TOKEN / SSO_PROVIDER")]
    end

    PROV -->|SCIM 2.0 HTTP| ROUTER
    ROUTER --> AUTH
    AUTH -->|valid token| ENV
    ROUTER -->|CRUD + discovery| DB
```

### Component Relationships

```mermaid
classDiagram
    class scim_router {
        +APIRouter router
        +_require_scim_token(authorization)
        +_user_to_scim(u)
        +_extract_department(body)
        +_primary_email(body)
        +_scim_error(status, detail)
        +service_provider_config()
        +resource_types()
        +list_users(...)
        +get_user(user_id)
        +create_user(request)
        +replace_user(user_id, request)
        +patch_user(user_id, request)
        +delete_user(user_id)
        +list_groups()
    }

    class User {
        +id
        +email
        +name
        +is_active
        +account_status
        +sso_provider
        +sso_subject
        +department
        +created_at
        +hashed_password
    }

    class SessionLocal {
        +query()
        +commit()
        +rollback()
        +close()
    }

    class logger {
        +info(msg)
        +error(msg)
    }

    scim_router --> User : maps to / from SCIM JSON
    scim_router --> SessionLocal : database sessions
    scim_router --> logger : audit / error logging
```

---

## Data Flow

### Provisioning a New User

```mermaid
sequenceDiagram
    participant IdP as Enterprise IdP
    participant RT as scim_router
    participant Auth as _require_scim_token
    participant DB as db.database.SessionLocal
    participant User as db.models.User

    IdP->>RT: POST /scim/v2/Users
    RT->>Auth: Authorization: Bearer <token>
    Auth-->>RT: token valid
    RT->>RT: parse JSON body, extract email
    RT->>DB: query User by email
    DB-->>RT: existing or None
    alt user exists
        RT-->>IdP: 409 Conflict + existing user
    else new user
        RT->>User: create User(...)
        RT->>DB: add + commit
        DB-->>RT: persisted user
        RT-->>IdP: 201 Created + SCIM User resource
    end
```

### De-provisioning a User

```mermaid
sequenceDiagram
    participant IdP as Enterprise IdP
    participant RT as scim_router
    participant Auth as _require_scim_token
    participant DB as db.database.SessionLocal

    IdP->>RT: DELETE /scim/v2/Users/{id}
    RT->>Auth: Authorization: Bearer <token>
    Auth-->>RT: token valid
    RT->>DB: query User by id
    DB-->>RT: user record
    RT->>DB: is_active=False, account_status=suspended
    DB-->>RT: committed
    RT-->>IdP: 204 No Content
```

### Discovery Flow

```mermaid
sequenceDiagram
    participant IdP as Enterprise IdP
    participant RT as scim_router

    IdP->>RT: GET /scim/v2/ServiceProviderConfig
    RT-->>IdP: capabilities + auth scheme

    IdP->>RT: GET /scim/v2/ResourceTypes
    RT-->>IdP: [User, Group]
```

---

## Process Flows

### Authentication Gate

```mermaid
flowchart LR
    A["Authorization header"] --> B{"starts with Bearer ?"}
    B -->|no| C["401 Invalid token"]
    B -->|yes| D["extract presented token"]
    D --> E{"SCIM_TOKEN set?"}
    E -->|no| F["503 SCIM not enabled"]
    E -->|yes| G["hmac.compare_digest"]
    G -->|mismatch| C
    G -->|match| H["continue to handler"]
```

### PATCH User Activation / Deactivation

```mermaid
flowchart TD
    A["PATCH /Users/{id}"] --> B["Parse Operations array"]
    B --> C["For each op"]
    C --> D{"op in replace/add?"}
    D -->|no| C
    D -->|yes| E["Read path/value"]
    E --> F{"path == active or value.active?"}
    F -->|yes| G["Set is_active"]
    G --> H["Update account_status"]
    H --> I["Commit + return user"]
    F -->|no| J{"path == displayName or value.displayName?"}
    J -->|yes| K["Update name"]
    K --> I
    J -->|no| C
```

---

## Integration with the Overall System

The `scim_router` sits alongside the other shared API routers and is mounted into the main FastAPI application. It is intentionally isolated from the user-facing JWT authentication flow and uses its own environment-based bearer token.

```mermaid
flowchart TB
    subgraph SharedRouters["Shared API Routers"]
        direction TB
        SCIM["scim_router<br/>(this module)"]
        AUTH["auth_router"]
        USERS["users / admin routers"]
        SSO["sso.py"]
    end

    subgraph MainApp["FastAPI Main App"]
        APP["app/main.py"]
    end

    subgraph DataLayer["Data Layer"]
        DB[("PostgreSQL")]
        MODELS["db/models.py"]
    end

    APP --> SCIM
    APP --> AUTH
    APP --> USERS
    SCIM --> MODELS
    AUTH --> SSO
    AUTH --> MODELS
    MODELS --> DB
```

### Related Modules

- **[auth_router](auth_router.md)**: Handles user login, registration, SSO callbacks, JWT sessions, and refresh tokens. SCIM-provisioned users authenticate through the SSO path configured there.
- **[db/models.py](db_models.md)** (referenced as `db.models.User`): Defines the `User` table schema that SCIM maps to and from.
- **[db/database.py](db_database.md)**: Provides `SessionLocal` used for transactional database access.
- **[core/logger.py](core_logger.md)**: Structured logging used for provisioning and de-provisioning audit events.
- **[sso.py](sso.md)** (in `auth/`): Implements the actual SSO provider integration for SCIM-provisioned users.

---

## Configuration

| Environment Variable | Purpose | Required |
|----------------------|---------|----------|
| `SCIM_TOKEN` | Shared bearer secret for IdP authentication | Yes (surface disabled if unset) |
| `SSO_PROVIDER` | Default SSO provider name for newly provisioned users | No (defaults to `"scim"`) |

---

## Error Handling

All errors are returned as SCIM-compliant JSON with the `urn:ietf:params:scim:api:messages:2.0:Error` schema:

```json
{
  "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
  "detail": "User not found",
  "status": "404"
}
```

Common status codes:

| Status | Meaning |
|--------|---------|
| 201 | User created |
| 204 | User de-provisioned |
| 401 | Invalid SCIM bearer token |
| 404 | User not found |
| 409 | User already exists (returns resource) |
| 503 | SCIM provisioning not enabled |

---

## Notes for Maintainers

- The router uses ad-hoc `SessionLocal()` imports inside handlers to avoid circular imports at module load time.
- Group membership is not expanded; IdPs should treat groups as informational department buckets.
- Filter support is intentionally minimal (`userName eq "value"`). Extend `_primary_email` / filter parsing if broader SCIM filter support is required.
- Deletion is always a soft deactivation. Hard deletion of financial-platform user records is intentionally not supported through this surface.
