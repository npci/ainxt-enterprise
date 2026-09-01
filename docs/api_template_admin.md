# `api_template_admin` - Optional Template Editor API

## Brief Introduction

`api_template_admin` is a feature-flagged FastAPI router that exposes administrative endpoints for editing the workflow template catalog from the ABStudio UI. It is the **only** write-path for seed templates and is deliberately isolated under the `/template-admin/` prefix so it does not collide with the read-only template routes in [`api_templates`](api_templates.md).

The module is optional: when the environment variable `TEMPLATES_EDITABLE` is not truthy, the router is still mounted but every endpoint returns `404 Not Found`, making the disabled state indistinguishable from a missing route.

---

## Core Functionality

The editor supports the full lifecycle of a catalog template:

| Operation | Endpoint | Purpose |
|-----------|----------|---------|
| Create | `POST /template-admin` | Add a brand-new template row and persist it to the seed sidecar in [`core_workflow_repo`](core_workflow_repo.md). |
| Update | `PUT /template-admin/{id}` | Edit `name`, `description`, `category`, `pattern`, `hitl`, or `graphData`. |
| Delete | `DELETE /template-admin/{id}` | Remove a template row from the database. |
| Save to seed | `POST /template-admin/{id}/save-to-seed` | Rewrite the matching entry in `_SEED_TEMPLATES` so the DB state becomes the new code-level baseline. |
| Reset | `POST /template-admin/{id}/reset` | Restore the row to its current `_SEED_TEMPLATES` definition (re-inserts if deleted). |
| Export snapshot | `GET /template-admin/export-snapshot` | Dump all visible DB rows for manual diff/merge back into `workflow_repo.py`. |
| Status probe | `GET /template-admin/status` | Public probe returning whether the editor is enabled. |

All mutating endpoints require an admin user via [`require_admin`](api_deps.md). The status endpoint is public so the frontend can decide whether to render edit controls without attempting a privileged call.

---

## Architecture

### Module Placement

```mermaid
flowchart TB
    subgraph Frontend["ABStudio Frontend"]
        TCM[TemplateCardMenu]
        TEM[TemplateEditModal]
        TCRM[TemplateCreateModal]
    end

    subgraph API["ABStudio Backend API"]
        TA["api_template_admin<br/>(write / admin)"]
        TR["api_templates<br/>(read / use / reseed)"]
        AD["api_deps<br/>(require_admin / require_access)"]
    end

    subgraph Core["Core Layer"]
        WR["core_workflow_repo<br/>(templates table + _SEED_TEMPLATES)"]
    end

    subgraph Store["Persistence"]
        DB[(Postgres templates table)]
        SRC[workflow_repo.py source<br/>_SEED_TEMPLATES literal]
    end

    TCM -->|edit / delete / save-to-seed / reset| TA
    TCRM -->|create| TA
    TEM -->|update| TA
    TR -->|list / get / use| WR
    TA -->|require_admin| AD
    TA -->|update_template / delete_template / create_template / save_template_to_seed / reset_template_to_seed / export_templates_snapshot| WR
    WR -->|read/write rows| DB
    WR -->|patch _SEED_TEMPLATES| SRC
```

### Why a Separate Router?

The read path (`GET /templates/{id}`) lives in [`api_templates`](api_templates.md). Putting write operations in the same file would have created route collisions and mixed admin-only mutations with public read operations. The `/template-admin/` prefix keeps the API surface explicit and lets operators disable the entire editor by unsetting one flag.

---

## Component Relationships

### Route Handlers

```mermaid
flowchart LR
    subgraph Routes["template_admin.py routes"]
        CREATE[create_template_route]
        UPDATE[update_template_route]
        DELETE[delete_template_route]
        SAVE[save_to_seed_route]
        RESET[reset_template_route]
        EXPORT[export_snapshot_route]
        STATUS[status_route]
    end

    subgraph Guards["Guards"]
        ENABLED[_require_enabled]
        ADMIN[require_admin]
    end

    subgraph Repo["workflow_repo functions"]
        CT[create_template]
        UT[update_template]
        DT[delete_template]
        STS[save_template_to_seed]
        RTS[reset_template_to_seed]
        ETS[export_templates_snapshot]
    end

    CREATE --> ENABLED --> ADMIN --> CT
    UPDATE --> ENABLED --> ADMIN --> UT
    DELETE --> ENABLED --> ADMIN --> DT
    SAVE --> ENABLED --> ADMIN --> STS
    RESET --> ENABLED --> ADMIN --> RTS
    EXPORT --> ENABLED --> ADMIN --> ETS
    STATUS --> ENABLED
```

### Data Flow: Update + Save-to-Seed

```mermaid
sequenceDiagram
    autonumber
    actor U as Admin User
    participant F as TemplateEditModal
    participant TA as update_template_route
    participant WR as workflow_repo.update_template
    participant DB as Postgres
    participant SS as save_to_seed_route
    participant SE as workflow_repo._upsert_seed_entry
    participant SRC as workflow_repo.py

    U->>F: Edit name / graph / pattern / hitl
    F->>TA: PUT /template-admin/{id}
    TA->>WR: update_template(id, data)
    WR->>DB: UPDATE templates ... RETURNING ...
    DB-->>WR: updated row
    WR-->>TA: row dict
    TA-->>F: 200 OK

    U->>F: Click "Save to seed"
    F->>SS: POST /template-admin/{id}/save-to-seed
    SS->>SE: save_template_to_seed(id)
    SE->>DB: SELECT current row
    DB-->>SE: row
    SE->>SE: format entry as source
    SE->>SRC: read -> patch -> atomic replace
    SE->>SE: update in-memory _SEED_TEMPLATES
    SE-->>SS: saved dict
    SS-->>F: 200 OK
```

### Data Flow: Reset to Seed

```mermaid
sequenceDiagram
    autonumber
    actor U as Admin User
    participant F as TemplateCardMenu
    participant RT as reset_template_route
    participant WR as workflow_repo.reset_template_to_seed
    participant MEM as _SEED_TEMPLATES in memory
    participant DB as Postgres

    U->>F: Click "Reset"
    F->>RT: POST /template-admin/{id}/reset
    RT->>WR: reset_template_to_seed(id)
    WR->>MEM: lookup seed by id
    MEM-->>WR: seed dict
    WR->>DB: INSERT ... ON CONFLICT UPDATE
    DB-->>WR: restored row
    WR-->>RT: row dict
    RT-->>F: 200 OK
```

---

## Feature Flag Behavior

```mermaid
flowchart TD
    A[Incoming request] --> B{/_is_enabled/}
    B -->|TEMPLATES_EDITABLE = 1/true/yes/on| C[Proceed to admin auth]
    B -->|anything else| D[HTTP 404 Not Found]
    C --> E{Admin?}
    E -->|Yes| F[Execute workflow_repo call]
    E -->|No| G[HTTP 403 Forbidden]
```

The `_is_enabled()` helper checks `TEMPLATES_EDITABLE` case-insensitively. `_require_enabled()` raises `HTTPException(404)` before any auth check, ensuring the flag-off state leaks no information about the endpoint's existence.

---

## Security & Authorization

- **Feature flag**: entire router is a no-op when disabled.
- **Admin-only**: all mutating routes depend on [`require_admin`](api_deps.md), which itself depends on [`require_access`](api_deps.md) / `_wrapped_gateway_auth` to produce an [`AuthenticatedUser`](app_models.md).
- **Hidden templates**: [`core_workflow_repo`](core_workflow_repo.md) rejects edits/deletes for `HIDDEN_TEMPLATE_IDS`, returning `404` to the caller.
- **Field whitelist**: `update_template` only accepts `name`, `description`, `category`, `pattern`, `hitl`, and `graphData`; unknown fields are silently ignored.

---

## Error Handling

| Scenario | HTTP Status | Source |
|----------|-------------|--------|
| Editor disabled | 404 | `_require_enabled` |
| Non-admin caller | 403 | `require_admin` |
| Template not found | 404 | `workflow_repo` returns `None` |
| Invalid create payload / duplicate id | 400 | `create_template` returns `None` |
| Template not in seed (reset) | 404 | `reset_template_to_seed` returns `None` |
| Unexpected repository exception | 500 | caught in route, logged with `[AGENT]` prefix |

All unexpected errors are logged via `core.logger.logger` and surfaced as a generic `500` detail to avoid leaking internals.

---

## Integration with the Seed System

The most important design detail is the two-way relationship with [`core_workflow_repo`](core_workflow_repo.md):

1. **DB-first edits**: `update_template` and `delete_template` only touch Postgres.
2. **Seed persistence**: `create_template` and `save_template_to_seed` call `_upsert_seed_entry`, which:
   - Reads `workflow_repo.py` from disk.
   - Locates the `_SEED_TEMPLATES` list bounds.
   - Replaces the existing entry by `id` or appends a new one.
   - Writes to a temp file, fsyncs, and atomically replaces the original via `os.replace`.
   - Updates the in-memory `_SEED_TEMPLATES` and `_TEMPLATES_BY_ID` caches so the running process sees the change immediately.

This makes the catalog under version control reflect exactly what the editor produces, and keeps saved edits across a DB wipe. See [`core_workflow_repo`](core_workflow_repo.md) for the full seed-management implementation.

---

## How It Fits Into the Overall System

- **Template consumers**: [`api_templates`](api_templates.md) (`list_templates`, `get_template_route`, `use_template_route`) and [`api_workflows`](api_workflows.md) read from the same `templates` table populated by this module.
- **Publishing path**: [`core_workflow_repo.publish_workflow_as_template`](core_workflow_repo.md) can also create catalog rows from approved workflows; `api_template_admin` is the manual/admin counterpart.
- **Reseeding**: [`api_templates.reseed_templates_route`](api_templates.md) inserts missing seed IDs but never overwrites existing rows, so admin edits are preserved until explicitly reset.
- **Startup**: [`app_main._lifespan`](app_main.md) initializes the DB and seeds canonical tools/skills; template seeding is handled by the same repository layer.

---

## Removal Recipe

To completely remove the optional editor:

1. Delete `ABStudio/backend/app/api/template_admin.py`.
2. In `app/main.py`, remove the `template_admin` import and its `include_router` entry.
3. (Optional) Delete the "Optional template editor support" block in `app/core/workflow_repo.py` (look for `_EDITABLE_TEMPLATE_FIELDS`).
4. Unset `TEMPLATES_EDITABLE` in the environment.

The read path, seed path, and `use_template` flow have zero dependencies on this module.

---

## Related Documentation

- [`api_templates`](api_templates.md) - read-only template listing, retrieval, use, and reseed.
- [`core_workflow_repo`](core_workflow_repo.md) - template persistence, seed management, and `_SEED_TEMPLATES` patching.
- [`api_deps`](api_deps.md) - authentication and authorization dependencies (`require_access`, `require_admin`).
- [`app_models`](app_models.md) - `AuthenticatedUser` model.
- [`app_main`](app_main.md) - application lifespan and router registration.
